import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
from telegram.error import BadRequest, TelegramError
import config
from database import Database
import logging
import asyncio
import httpx

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
        self.semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent API calls
        
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
                    text="I'm now an admin! 🎉\nAvailable command:\n/index - Index all media/documents in this group"
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
            # Send a dummy message to get a reference point
            async with self.semaphore:
                dummy_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text="Indexing in progress..."
                )
            latest_message_id = dummy_message.message_id
            
            while latest_message_id > 1:
                logger.info(f"Fetching messages for chat {chat_id}, up to message_id: {latest_message_id}")
                async with self.semaphore:
                    # Fetch messages by sending messages with reply_to_message_id
                    messages = []
                    for i in range(100):
                        target_id = latest_message_id - i
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
                
                if batch_processed == 0 or len(messages) < 100:
                    logger.info("No new files processed or fewer than 100 messages, stopping")
                    break
                    
                latest_message_id = min(m['message_id'] for m in messages)
                logger.info(f"Pausing 2 seconds before next batch, next max message_id: {latest_message_id}")
                await asyncio.sleep(2)  # Rate limit between batches
                
            # Clean up dummy message
            async with self.semaphore:
                await context.bot.delete_message(chat_id=chat_id, message_id=dummy_message.message_id)
                
            logger.info(f"Indexing complete for chat {chat_id}, {processed} files indexed")
            async with self.semaphore:
                await status_message.edit_text(f"Indexing complete! {processed} files indexed.")
            
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
                    text=f"Telegram API error during indexing: {str(e)}. Ensure the bot has permission to read chat history."
                )
        except Exception as e:
            logger.error(f"Unexpected error during indexing: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Unexpected error during indexing: {str(e)}"
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
        app = Application.builder().token(config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("index", self.index))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        
        logger.info("Running polling")
        app.run_polling(poll_interval=2.0, timeout=30)

if __name__ == '__main__':
    bot = TelegramBot()
    bot.run()
