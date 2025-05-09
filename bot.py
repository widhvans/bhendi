import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, filters, CallbackContext
from telegram import Update
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
        self.rate_limit_delay = 2  # Delay between batches in seconds
        
    def start(self, update: Update, context: CallbackContext):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
        chat_id = update.effective_chat.id
        admins = context.bot.get_chat_administrators(chat_id)
        bot_id = context.bot.id
        is_admin = any(admin.user.id == bot_id for admin in admins)
        
        if is_admin:
            update.message.reply_text(
                "I'm now an admin! 🎉\n"
                "Available command:\n"
                "/index - Index all media/documents in this group"
            )
    
    def index(self, update: Update, context: CallbackContext):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        admins = context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            update.message.reply_text("Only admins can use /index command!")
            return
            
        status_message = update.message.reply_text("Starting indexing process... [0 files indexed]")
        context.job_queue.run_once(self._index_messages, 0, data={
            'chat_id': chat_id,
            'status_message': status_message,
            'processed': 0
        })
    
    def _index_messages(self, context: CallbackContext):
        job = context.job
        chat_id = job.data['chat_id']
        status_message = job.data['status_message']
        processed = job.data['processed']
        
        try:
            # Process messages in batches
            messages = context.bot.get_chat_history(chat_id=chat_id, limit=100)
            batch_processed = 0
            
            for message in messages:
                if self.process_message(message, chat_id):
                    batch_processed += 1
                    processed += 1
                    
                    if processed % 100 == 0:
                        status_message.edit_text(f"Indexing... [{processed} files indexed]")
                        # Yield control to event loop
                        asyncio.get_event_loop().run_until_complete(asyncio.sleep(self.rate_limit_delay))
            
            if batch_processed > 0:
                # Schedule next batch
                context.job_queue.run_once(self._index_messages, self.rate_limit_delay, data={
                    'chat_id': chat_id,
                    'status_message': status_message,
                    'processed': processed
                })
            else:
                status_message.edit_text(f"Indexing complete! {processed} files indexed.")
                
        except Exception as e:
            logger.error(f"Indexing error: {str(e)}")
            status_message.edit_text(f"Error during indexing: {str(e)}")
    
    def process_message(self, message, chat_id):
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
    
    def handle_message(self, update: Update, context: CallbackContext):
        if update.effective_chat.type not in ['group', 'supergroup']:
            return
            
        chat_id = update.effective_chat.id
        message = update.message
        
        # Handle new files
        if message.document or message.photo or message.video or message.audio:
            self.process_message(message, chat_id)
            return
            
        # Handle search queries
        if message.text:
            search_term = message.text.lower()
            files = self.db.search_files(chat_id, search_term)
            
            if files:
                for file in files:
                    context.bot.send_document(
                        chat_id=chat_id,
                        document=file['file_id'],
                        caption=f"Found: {file['file_name']}"
                    )
            else:
                admins = context.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if admin.user.id != context.bot.id:  # Don't PM the bot itself
                        context.bot.send_message(
                            chat_id=admin.user.id,
                            text=f"File '{search_term}' not found in group {update.effective_chat.title}. Requested by @{update.effective_user.username}"
                        )
                update.message.reply_text(
                    f"File '{search_term}' not found! Notification sent to admins."
                )
    
    def run(self):
        updater = Updater(config.BOT_TOKEN)
        
        dp = updater.dispatcher
        dp.add_handler(CommandHandler("start", self.start))
        dp.add_handler(CommandHandler("index", self.index))
        dp.add_handler(MessageHandler(filters.all & ~filters.command, self.handle_message))
        
        updater.start_polling(poll_interval=2.0, timeout=30)
        updater.idle()

if __name__ == '__main__':
    bot = TelegramBot()
    bot.run()
