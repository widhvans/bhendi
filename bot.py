import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
from telegram.error import BadRequest
import config
from database import Database
import logging
import asyncio
import httpx

# Configure logging to show only necessary logs
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self):
        self.db = Database()
        self.semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent API calls
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
        chat_id = update.effective_chat.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        bot_id = context.bot.id
        is_admin = any(admin.user.id == bot_id for admin in admins)
        
        if is_admin:
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="I'm now an admin! 🎉\nAvailable command:\n/index - Index all media/documents in this group"
                )
    
    async def index(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        async with self.semaphore:
            admins = await context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Only admins can use /index command!"
                )
            return
            
        async with self.semaphore:
            status_message = await context.bot.send_message(
                chat_id=chat_id,
                text="Starting indexing process... [0 files indexed]"
            )
        processed = 0
        
        try:
            offset = 0
            while True:
                async with self.semaphore:
                    updates = await context.bot.get_updates(offset=offset, timeout=30)
                if not updates:
                    break
                    
                batch_processed = 0
                for update in updates:
                    offset = max(offset, update.update_id + 1)
                    if update.message and update.message.chat_id == chat_id:
                        if await self.process_message(update.message, chat_id):
                            batch_processed += 1
                            processed += 1
                            
                            if processed % 100 == 0:
                                async with self.semaphore:
                                    await status_message.edit_text(f"Indexing... [{processed} files indexed]")
                                await asyncio.sleep(10)  # Update every 10 seconds or 100 files
                
                if batch_processed == 0 and len(updates) < 100:
                    break
                    
                await asyncio.sleep(2)  # Rate limit between batches
                
            async with self.semaphore:
                await status_message.edit_text(f"Indexing complete! {processed} files indexed.")
            
        except BadRequest as e:
            logger.error(f"BadRequest during indexing: {str(e)}")
            async with self.semaphore:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Error during indexing: {str(e)}"
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
                self.db.save_file(chat_id, file_name, file_id)
                return True
        return False
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
            
        chat_id = update.effective_chat.id
        message = update.message
        
        # Handle new files
        if message.document or message.photo or message.video or message.audio:
            await self.process_message(message, chat_id)
            return
            
        # Handle search queries
        if message.text:
            search_term = message.text.lower()
            files = self.db.search_files(chat_id, search_term)
            
            if files:
                for file in files:
                    async with self.semaphore:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=file['file_id'],
                            caption=f"Found: {file['file_name']}"
                        )
            else:
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
        app = Application.builder().token(config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("index", self.index))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        
        app.run_polling(poll_interval=2.0, timeout=30)

if __name__ == '__main__':
    bot = TelegramBot()
    bot.run()
