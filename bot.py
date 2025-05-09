import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
import config
from database import Database
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self):
        self.db = Database()
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat.type in ['group', 'supergroup']:
            return
        # Check if bot is admin
        chat_id = update.effective_chat.id
        admins = await context.bot.get_chat_administrators(chat_id)
        bot_id = context.bot.id
        is_admin = any(admin.user.id == bot_id for admin in admins)
        
        if is_admin:
            await update.message.reply_text(
                "I'm now an admin! 🎉\n"
                "Available command:\n"
                "/index - Index all media/documents in this group"
            )
    
    async def index(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat.type in ['group', 'supergroup']:
            return
            
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        admins = await context.bot.get_chat_administrators(chat_id)
        
        if not any(admin.user.id == user_id for admin in admins):
            await update.message.reply_text("Only admins can use /index command!")
            return
            
        await update.message.reply_text("Starting indexing process... This might take a while.")
        
        async for message in context.bot.get_chat_history(chat_id=chat_id):
            await self.process_message(message, chat_id)
            
        await update.message.reply_text("Indexing complete! All media/documents have been saved.")
    
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
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat.type in ['group', 'supergroup']:
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
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=file['file_id'],
                        caption=f"Found: {file['file_name']}"
                    )
            else:
                admins = await context.bot.get_chat_administrators(chat_id)
                admin_mentions = ' '.join([f"@{admin.user.username}" for admin in admins if admin.user.username])
                await message.reply_text(
                    f"File '{search_term}' not found! {admin_mentions} please upload this file."
                )
    
    def run(self):
        app = Application.builder().token(config.BOT_TOKEN).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("index", self.index))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        
        app.run_polling()

if __name__ == '__main__':
    bot = TelegramBot()
    bot.run()
