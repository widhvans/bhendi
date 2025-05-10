import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from database import Database
import config
import asyncio
import re
from datetime import datetime, timedelta
from logger import get_logger

class TelegramBot:
    def __init__(self):
        self.logger = get_logger('TelegramBot')
        self.db = Database()
        self.app = Application.builder().token(config.BOT_TOKEN).build()
        self.last_update = datetime.now()
        self.indexing_requests = {}  # Store user_id: chat_id for /index command
        self.logger.info("Bot initialized")

    async def start(self, update, context):
        self.logger.info(f"Start command received from user {update.message.from_user.id}")
        await update.message.reply_text("Bot started! Send file names to search, forward group/channel files to index, or use /index to index all files from a chat.")

    async def index_command(self, update, context):
        user_id = update.message.from_user.id
        if update.message.chat.type != 'private':
            self.logger.debug(f"Ignoring /index command from non-private chat {update.message.chat_id}")
            await update.message.reply_text("Please use /index in my private chat.")
            return
        self.indexing_requests[user_id] = None
        self.logger.info(f"/index command received from user {user_id}")
        await update.message.reply_text("Please forward the last message from the group/channel where I'm an admin to start indexing all files.")

    async def handle_message(self, update, context):
        message = update.message or update.channel_post
        if not message:
            self.logger.debug("Invalid update: no message or channel post")
            return

        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else None
        is_private = message.chat.type == 'private'
        is_group_or_channel = message.chat.type in ['group', 'supergroup', 'channel']

        # Handle /index forwarded message in private chat
        if is_private and user_id in self.indexing_requests and (message.forward_from_chat or message.forward_from):
            forward_chat = message.forward_from_chat
            if forward_chat and forward_chat.type in ['group', 'supergroup', 'channel']:
                try:
                    bot_member = await context.bot.get_chat_member(forward_chat.id, context.bot.id)
                    if bot_member.status == 'administrator':
                        self.indexing_requests[user_id] = forward_chat.id
                        self.logger.info(f"User {user_id} forwarded message from chat {forward_chat.id} for indexing")
                        await update.message.reply_text(f"Starting to index all files from chat {forward_chat.id}...")
                        await self.index_all_files(context, forward_chat.id, user_id, message.message_id)
                        del self.indexing_requests[user_id]
                    else:
                        self.logger.warning(f"Bot is not admin in forwarded chat {forward_chat.id}")
                        await update.message.reply_text("I'm not an admin in that chat.")
                        del self.indexing_requests[user_id]
                except telegram.error.Forbidden as e:
                    self.logger.error(f"Cannot access forwarded chat {forward_chat.id}: {str(e)}")
                    await update.message.reply_text("I cannot access that chat.")
                    del self.indexing_requests[user_id]
            return

        # Handle forwarded messages in private chats
        if is_private and (message.forward_from_chat or message.forward_from):
            forward_chat = message.forward_from_chat
            if forward_chat and forward_chat.type in ['group', 'supergroup', 'channel']:
                try:
                    bot_member = await context.bot.get_chat_member(forward_chat.id, context.bot.id)
                    if bot_member.status == 'administrator':
                        if message.document or message.video or message.audio or message.photo:
                            await self.index_file(message, context, forward_chat.id, is_forwarded=True, user_id=user_id)
                except telegram.error.Forbidden as e:
                    self.logger.error(f"Cannot access forwarded chat {forward_chat.id}: {str(e)}")
                    return
            return

        # Handle direct messages in groups/channels
        if not is_group_or_channel:
            self.logger.debug(f"Ignoring message from non-group/channel chat {chat_id}")
            return

        try:
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if bot_member.status != 'administrator':
                self.logger.warning(f"Bot is not admin in chat {chat_id}")
                return
        except telegram.error.Forbidden as e:
            self.logger.error(f"Cannot access chat {chat_id}: {str(e)}")
            return

        if message.document or message.video or message.audio or message.photo:
            await self.index_file(message, context, chat_id, is_forwarded=False)

        if message.text:
            await self.handle_search(message, context)

    async def index_file(self, message, context, target_chat_id, is_forwarded=False, user_id=None):
        chat_id = message.chat_id
        file_info = {}

        try:
            if message.document:
                file_info = {
                    'type': 'document',
                    'name': message.document.file_name,
                    'file_id': message.document.file_id,
                    'size': message.document.file_size,
                    'chat_id': target_chat_id,
                    'message_id': message.message_id,
                    'timestamp': message.date,
                    'forwarded': is_forwarded
                }
            elif message.video:
                file_info = {
                    'type': 'video',
                    'name': message.video.file_name or f"video_{message.video.file_id}",
                    'file_id': message.video.file_id,
                    'size': message.video.file_size,
                    'chat_id': target_chat_id,
                    'message_id': message.message_id,
                    'timestamp': message.date,
                    'forwarded': is_forwarded
                }
            elif message.audio:
                file_info = {
                    'type': 'audio',
                    'name': message.audio.file_name or f"audio_{message.audio.file_id}",
                    'file_id': message.audio.file_id,
                    'size': message.audio.file_size,
                    'chat_id': target_chat_id,
                    'message_id': message.message_id,
                    'timestamp': message.date,
                    'forwarded': is_forwarded
                }
            elif message.photo:
                file_info = {
                    'type': 'photo',
                    'name': f"photo_{message.photo[-1].file_id}",
                    'file_id': message.photo[-1].file_id,
                    'size': message.photo[-1].file_size,
                    'chat_id': target_chat_id,
                    'message_id': message.message_id,
                    'timestamp': message.date,
                    'forwarded': is_forwarded
                }

            if file_info:
                # Skip duplicate check for forwarded files
                if not is_forwarded and self.db.file_exists(file_info['file_id']):
                    self.logger.debug(f"Skipped duplicate file {file_info['name']} (ID: {file_info['file_id']}) in chat {target_chat_id}")
                    return

                # Remove existing file with same file_id to avoid duplicate key error
                if is_forwarded:
                    self.db.files.delete_one({'file_id': file_info['file_id']})

                self.db.save_file(file_info)
                self.logger.info(f"Indexed file {file_info['name']} (ID: {file_info['file_id']}) in chat {target_chat_id}{' (forwarded)' if is_forwarded else ''}")
                await context.bot.send_message(target_chat_id, "✅")
                if user_id:
                    await self.update_indexing_status(context, target_chat_id, user_id)
                else:
                    await self.update_indexing_status(context, target_chat_id)
        except Exception as e:
            self.logger.error(f"Error indexing file in chat {target_chat_id}: {str(e)}")

    async def index_all_files(self, context, chat_id, user_id, last_message_id):
        try:
            message_id = last_message_id
            messages_processed = 0
            max_messages = 100  # Limit to avoid rate limits

            while messages_processed < max_messages:
                try:
                    message = await context.bot.get_message(chat_id, message_id)
                    if message.document or message.video or message.audio or message.photo:
                        await self.index_file(message, context, chat_id, is_forwarded=True, user_id=user_id)
                    message_id -= 1
                    messages_processed += 1
                except telegram.error.BadRequest as e:
                    if "Message_id_invalid" in str(e):
                        break  # No more messages
                    self.logger.warning(f"Error fetching message {message_id} in chat {chat_id}: {str(e)}")
                    message_id -= 1
                    messages_processed += 1
                except Exception as e:
                    self.logger.error(f"Error processing message {message_id} in chat {chat_id}: {str(e)}")
                    break

            self.logger.info(f"Completed indexing {messages_processed} messages for chat {chat_id}")
            await context.bot.send_message(user_id, f"Finished indexing files for chat {chat_id}. Processed {messages_processed} messages.")
        except Exception as e:
            self.logger.error(f"Error indexing all files for chat {chat_id}: {str(e)}")
            await context.bot.send_message(user_id, f"Error indexing files for chat {chat_id}: {str(e)}")

    async def handle_search(self, message, context):
        chat_id = message.chat_id
        query = message.text.strip()
        if not query:
            return

        self.logger.info(f"Search request for '{query}' in chat {chat_id}")

        try:
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
                    self.logger.info(f"Sent file {file['name']} (ID: {file['file_id']}) for query '{query}'")
            else:
                admins = await context.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if not admin.user.is_bot:  # Skip bot admins
                        try:
                            await context.bot.send_message(
                                admin.user.id,
                                f"File '{query}' not found in chat {chat_id}"
                            )
                            self.logger.info(f"Notified admin {admin.user.id} about missing file '{query}'")
                        except telegram.error.Forbidden as e:
                            self.logger.warning(f"Cannot notify admin {admin.user.id}: {str(e)}")
                await message.reply_text(f"No files found matching '{query}'.")
                self.logger.info(f"No files found for query '{query}' in chat {chat_id}")
        except Exception as e:
            self.logger.error(f"Error handling search for '{query}' in chat {chat_id}: {str(e)}")

    async def update_indexing_status(self, context, chat_id, user_id=None):
        now = datetime.now()
        if now - self.last_update >= timedelta(seconds=10):
            count = self.db.get_file_count(chat_id)
            target_id = user_id if user_id else chat_id
            await context.bot.send_message(target_id, f"Indexing in progress for chat {chat_id}... {count} files indexed.")
            self.logger.info(f"Updated indexing status for chat {chat_id}: {count} files")
            self.last_update = now

    async def error_handler(self, update, context):
        self.logger.error(f"Update {update} caused error: {context.error}")

    def run(self):
        self.logger.info("Starting bot polling")
        try:
            self.app.add_handler(CommandHandler("start", self.start))
            self.app.add_handler(CommandHandler("index", self.index_command))
            self.app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
            self.app.add_error_handler(self.error_handler)
            self.app.run_polling()
        except Exception as e:
            self.logger.error(f"Error running bot: {str(e)}")

if __name__ == "__main__":
    bot = TelegramBot()
    bot.run()
