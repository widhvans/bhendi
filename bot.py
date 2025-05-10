import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from database import Database
import config
import asyncio
import re
from datetime import datetime, timedelta

class TelegramBot:
    def __init__(self):
        self.db = Database()
        self.app = Application.builder().token(config.BOT_TOKEN).build()
        self.last_update = datetime.now()

    async def start(self, update, context):
        await update.message.reply_text("Bot started! I index files in group chats and allow file searches.")

    async def handle_message(self, update, context):
        if not update.message.chat.type in ['group', 'supergroup', 'channel']:
            return

        chat_id = update.message.chat_id
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status != 'administrator':
            return

        if update.message.document or update.message.video or update.message.audio or update.message.photo:
            await self.index_file(update, context)

        if update.message.text:
            if await self.is_search_request(update.message.text):
                await self.handle_search(update, context)

    async def index_file(self, update, context):
        chat_id = update.message.chat_id
        message = update.message
        file_info = {}

        if message.document:
            file_info = {
                'type': 'document',
                'name': message.document.file_name,
                'file_id': message.document.file_id,
                'size': message.document.file_size,
                'chat_id': chat_id,
                'message_id': message.message_id,
                'timestamp': message.date
            }
        elif message.video:
            file_info = {
                'type': 'video',
                'name': message.video.file_name or f"video_{message.video.file_id}",
                'file_id': message.video.file_id,
                'size': message.video.file_size,
                'chat_id': chat_id,
                'message_id': message.message_id,
                'timestamp': message.date
            }
        elif message.audio:
            file_info = {
                'type': 'audio',
                'name': message.audio.file_name or f"audio_{message.audio.file_id}",
                'file_id': message.audio.file_id,
                'size': message.audio.file_size,
                'chat_id': chat_id,
                'message_id': message.message_id,
                'timestamp': message.date
            }
        elif message.photo:
            file_info = {
                'type': 'photo',
                'name': f"photo_{message.photo[-1].file_id}",
                'file_id': message.photo[-1].file_id,
                'size': message.photo[-1].file_size,
                'chat_id': chat_id,
                'message_id': message.message_id,
                'timestamp': message.date
            }

        if file_info and not self.db.file_exists(file_info['file_id']):
            self.db.save_file(file_info)
            await self.update_indexing_status(context, chat_id)

    async def is_search_request(self, text):
        return bool(re.match(r'^[!/]search\s+(.+)$', text, re.IGNORECASE))

    async def handle_search(self, update, context):
        chat_id = update.message.chat_id
        text = update.message.text
        match = re.match(r'^[!/]search\s+(.+)$', text, re.IGNORECASE)
        if not match:
            return

        query = match.group(1).strip()
        files = self.db.search_files(query)

        if files:
            for file in files:
                caption = f"{file['name']} ({file['type']})"
                if file['type'] == 'document':
                    await context.bot.send_document(chat_id, file['file_id'], caption=caption)
                elif file['type'] == 'video':
                    await context.bot.send_video(chat_id, file['file_id'], caption=caption)
                elif file['type'] == 'audio':
                    await context.bot.send_audio(chat_id, file['file_id'], caption=caption)
                elif file['type'] == 'photo':
                    await context.bot.send_photo(chat_id, file['file_id'], caption=caption)
        else:
            admins = await context.bot.get_chat_administrators(chat_id)
            for admin in admins:
                await context.bot.send_message(
                    admin.user.id,
                    f"File '{query}' not found in chat {chat_id}"
                )
            await update.message.reply_text(f"No files found matching '{query}'.")

    async def update_indexing_status(self, context, chat_id):
        now = datetime.now()
        if now - self.last_update >= timedelta(seconds=10):
            count = self.db.get_file_count(chat_id)
            await context.bot.send_message(chat_id, f"Indexing in progress... {count} files indexed.")
            self.last_update = now

    def run(self):
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
        self.app.run_polling()

if __name__ == "__main__":
    bot = TelegramBot()
    bot.run()
