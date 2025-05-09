import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update, MessageOrigin
from telegram.error import BadRequest, TelegramError
import config
from database import Database
import logging
import asyncio
import httpx
import io

# Configure logging to output to console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler('bot.log')  # File output
    ]
)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self):
        self.db = Database()
        self.semaphore = asyncio.Semaphore(1)  # Strict rate limiting to prevent pool timeout
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Received /start command")
        if update.effective_chat.type not in ['group', 'supergroup']:
            logger.info("Ignoring /start in non-group chat")
            return
        chat_id = update.effective_chat.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        bot_id = context.bot.id
        is_admin = any(admin.user.id == bot_id for admin in admins)
        
        if is_admin:
            logger.info(f"Bot is admin in chat {chat_id}, sending welcome message")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="I'm now an admin! 🎉\nAvailable commands:\n/index - Index media/documents\n/reindex - Reindex after sending old files\n/forward - Forward specific messages\n/status - Check indexing progress"
                )
    
    async def index(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Received /index command")
        if update.effective_chat.type not in ['group', 'supergroup']:
            logger.info("Ignoring /index in non-group chat")
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            logger.info(f"Non-admin user {user_id} tried /index in chat {chat_id}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Only admins can use /index command!"
                )
            return
            
        logger.info(f"Starting indexing for chat {chat_id}")
        async with self.semaphore:
            status_message = await context.bot.send_message(
                chat_id=chat_id,
                text="Starting indexing process... [0 files indexed]"
            )
        processed = 0
        
        try:
            # Check pinned message for media
            async with self.semaphore:
                chat = await context.bot.get_chat(chat_id)
                if chat.pinned_message:
                    logger.info(f"Processing pinned message in chat {chat_id}")
                    if await self.process_message(chat.pinned_message, chat_id):
                        processed += 1
                        logger.info("Indexed file from pinned message")
            
            # Post a dummy file as reference
            dummy_file = io.BytesIO(b"Dummy file for indexing")
            dummy_file.name = "index_reference.txt"
            async with self.semaphore:
                reference_message = await context.bot.send_document(
                    chat_id=chat_id,
                    document=dummy_file,
                    caption="Indexing reference file (will be deleted)"
                )
            reference_message_id = reference_message.message_id
            logger.info(f"Posted reference file with message_id {reference_message_id}")
            
            # Fetch messages backward from reference_message_id
            last_processed_id = self.db.get_last_indexed_message_id(chat_id) or reference_message_id
            current_message_id = min(reference_message_id, last_processed_id)
            while current_message_id > 1:
                logger.info(f"Fetching messages for chat {chat_id}, up to message_id: {current_message_id}")
                messages = []
                for i in range(50):  # Smaller batch to reduce API load
                    target_id = current_message_id - i
                    if target_id < 1:
                        break
                    try:
                        async with self.semaphore:
                            response = await context.bot._post(
                                'getMessage',
                                data={
                                    'chat_id': chat_id,
                                    'message_id': target_id
                                }
                            )
                        if response.get('ok'):
                            messages.append(response['result'])
                        else:
                            logger.warning(f"Failed to fetch message {target_id}: {response.get('description', 'Unknown error')}")
                    except TelegramError as e:
                        logger.warning(f"Failed to fetch message {target_id}: {str(e)}")
                        continue
                
                if not messages:
                    logger.info("No more messages fetched")
                    break
                    
                batch_processed = 0
                for message in messages:
                    message_obj = telegram.Message.de_json(message, context.bot)
                    if message_obj and await self.process_message(message_obj, chat_id):
                        batch_processed += 1
                        processed += 1
                        
                        if processed % 100 == 0:
                            logger.info(f"Processed {processed} files, updating status")
                            async with self.semaphore:
                                await status_message.edit_text(f"Indexing... [{processed} files indexed]")
                            await asyncio.sleep(10)  # Update every 10 seconds or 100 files
                
                if batch_processed == 0 or len(messages) < 50:
                    logger.info("No new files processed or fewer than 50 messages, stopping")
                    break
                    
                current_message_id = min(m['message_id'] for m in messages)
                self.db.save_indexed_message_id(chat_id, current_message_id)
                logger.info(f"Pausing 5 seconds before next batch, next message_id: {current_message_id}")
                await asyncio.sleep(5)  # Increased to 5 seconds to prevent pool timeout
            
            # Delete reference file
            try:
                async with self.semaphore:
                    await context.bot.delete_message(chat_id=chat_id, message_id=reference_message_id)
                logger.info(f"Deleted reference file with message_id {reference_message_id}")
            except TelegramError as e:
                logger.warning(f"Failed to delete reference file: {str(e)}")
                
            logger.info(f"Indexing complete for chat {chat_id}, {processed} files indexed")
            async with self.semaphore:
                await status_message.edit_text(f"Indexing complete! {processed} files indexed.")
                admin_mentions = ' '.join([f"@{admin.user.username}" for admin in admins if admin.user.username])
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{admin_mentions}, please send or forward a media file to index older files, then use /reindex or /forward <message_id>."
                )
            
        except BadRequest as e:
            logger.error(f"BadRequest during indexing: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Error during indexing: {str(e)}"
                )
        except TelegramError as e:
            logger.error(f"Telegram API error during indexing: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Telegram API error during indexing: {str(e)}. Please send or forward a media file and use /reindex or /forward <message_id>."
                )
        except Exception as e:
            logger.error(f"Unexpected error during indexing: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Unexpected error during indexing: {str(e)}"
                )
    
    async def reindex(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Received /reindex command")
        await self.index(update, context)
    
    async def forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Received /forward command")
        if update.effective_chat.type not in ['group', 'supergroup']:
            logger.info("Ignoring /forward in non-group chat")
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            logger.info(f"Non-admin user {user_id} tried /forward in chat {chat_id}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Only admins can use /forward command!"
                )
            return
            
        if not context.args:
            logger.info("No message ID provided for /forward")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Please provide a message ID or link to forward, e.g., /forward 123 or /forward https://t.me/c/.../123"
                )
            return
            
        message_id = None
        try:
            arg = context.args[0]
            if arg.startswith('https://t.me/'):
                # Extract message ID from link
                message_id = int(arg.split('/')[-1])
            else:
                message_id = int(arg)
        except ValueError:
            logger.info(f"Invalid message ID format: {context.args[0]}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Invalid message ID or link format. Use /forward <message_id> or /forward <message_link>"
                )
            return
            
        logger.info(f"Attempting to index message {message_id} in chat {chat_id}")
        try:
            async with self.semaphore:
                response = await context.bot._post(
                    'getMessage',
                    data={
                        'chat_id': chat_id,
                        'message_id': message_id
                    }
                )
            if response.get('ok'):
                message_obj = telegram.Message.de_json(response['result'], context.bot)
                if message_obj and await self.process_message(message_obj, chat_id):
                    logger.info(f"Indexed file from forwarded message {message_id}")
                    async with self.semaphore:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"Indexed file from message {message_id}. Use /reindex to continue or /forward another message ID."
                        )
                else:
                    logger.info(f"No media found in message {message_id}")
                    async with self.semaphore:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"No media found in message {message_id}. Try another message ID or forward the message manually."
                        )
            else:
                logger.warning(f"Failed to fetch message {message_id}: {response.get('description', 'Unknown error')}")
                async with self.semaphore:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Could not access message {message_id}. Please forward the message manually or try another ID."
                    )
        except TelegramError as e:
            logger.error(f"Telegram API error fetching message {message_id}: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Error fetching message {message_id}: {str(e)}. Please forward the message manually."
                )
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Received /status command")
        if update.effective_chat.type not in ['group', 'supergroup']:
            logger.info("Ignoring /status in non-group chat")
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            logger.info(f"Non-admin user {user_id} tried /status in chat {chat_id}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Only admins can use /status command!"
                )
            return
            
        file_count = self.db.get_file_count(chat_id)
        last_message_id = self.db.get_last_indexed_message_id(chat_id) or 0
        logger.info(f"Status for chat {chat_id}: {file_count} files indexed, last message_id {last_message_id}")
        async with self.semaphore:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Indexing Status:\n- Files indexed: {file_count}\n- Last processed message ID: {last_message_id}"
            )
    
    async def process_message(self, message, chat_id):
        if message.document or message.photo or message.video or message.audio:
            file_name = None
            file_id = None
            
            if message.document:
                file_name = message.document.file_name
                file_id = message.document.file_id
            elif message.photo:
                file_name = f"photo_{message.message_id}.jpg"
                file_id = message.photo[-1].file_id
            elif message.video:
                file_name = message.video.file_name or f"video_{message.message_id}.mp4"
                file_id = message.video.file_id
            elif message.audio:
                file_name = message.audio.file_name or f"audio_{message.message_id}.mp3"
                file_id = message.audio.file_id
                
            if file_name and file_id:
                is_forwarded = isinstance(message.forward_origin, (MessageOrigin.User, MessageOrigin.Chat, MessageOrigin.Channel))
                if is_forwarded:
                    logger.info(f"Processing forwarded file: {file_name} with ID {file_id} for chat {chat_id}")
                else:
                    logger.info(f"Saving file: {file_name} with ID {file_id} for chat {chat_id}")
                self.db.save_file(chat_id, file_name, file_id)
                return True
        return False
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Handling new message")
        if update.effective_chat.type not in ['group', 'supergroup']:
            logger.info("Ignoring message in non-group chat")
            return
            
        chat_id = update.effective_chat.id
        message = update.message
        
        # Handle new files
        if message.document or message.photo or message.video or message.audio:
            logger.info(f"Processing new media file in chat {chat_id}")
            await self.process_message(message, chat_id)
            return
            
        # Handle search queries
        if message.text:
            search_term = message.text.lower()
            logger.info(f"Searching for term '{search_term}' in chat {chat_id}")
            files = self.db.search_files(chat_id, search_term)
            
            if files:
                logger.info(f"Found {len(files)} files matching '{search_term}'")
                for file in files:
                    async with self.semaphore:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=file['file_id'],
                            caption=f"Found: {file['file_name']}"
                        )
            else:
                logger.info(f"No files found for '{search_term}', notifying admins")
                async with self.semaphore:
                    admins = await context.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if admin.user.id != context.bot.id:  # Don't PM the bot itself
                        async with self.semaphore:
                            await context.bot.send_message(
                                chat_id=admin.user.id,
                                text=f"File '{search_term}' not found in group {update.effective_chat.title}. Requested by @{update.effective_user.username}"
                            )
                async with self.semaphore:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"File '{search_term}' not found! Notification sent to admins."
                    )
    
    def run(self):
        logger.info("Starting bot")
        app = Application.builder().token(config.BOT_TOKEN).concurrent_updates(10).connection_pool_size(30).pool_timeout(90).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("index", self.index))
        app.add_handler(CommandHandler("reindex", self.reindex))
        app.add_handler(CommandHandler("forward", self.forward))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        
        logger.info("Running polling")
        app.run_polling(poll_interval=3.0, timeout=30)

if __name__ == '__main__':
    bot = TelegramBot()
    bot.run()
