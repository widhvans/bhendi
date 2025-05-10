import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from database import Database
import config
import asyncio
import re
import requests
from datetime import datetime, timedelta
from logger import get_logger
from pytz import UTC

class TelegramBot:
    def __init__(self):
        self.logger = get_logger('TelegramBot')
        self.db = Database()
        self.app = Application.builder().token(config.BOT_TOKEN).build()
        self.last_update = datetime.now()
        self.indexing_requests = {}  # Store user_id: {chat_id, processed_ids}
        self.logger.info("Bot initialized")

    async def start(self, update, context):
        self.logger.info(f"Start command received from user {update.message.from_user.id}")
        await update.message.reply_text("Bot started! Send file names to search, forward group/channel files to index, or use /index to index previous files from a chat.")

    async def index_command(self, update, context):
        user_id = update.message.from_user.id
        if update.message.chat.type != 'private':
            self.logger.debug(f"Ignoring /index command from non-private chat {update.message.chat_id}")
            await update.message.reply_text("Please use /index in my private chat.")
            return
        self.indexing_requests[user_id] = {'chat_id': None, 'processed_ids': set()}
        self.logger.info(f"/index command received from user {user_id}")
        await update.message.reply_text("Please send a Telegram file link (e.g., https://t.me/c/1814841940/588956) from the group/channel where I'm an admin to start indexing previous files.")

    async def handle_message(self, update, context):
        message = update.message or update.channel_post
        if not message:
            self.logger.debug("Invalid update: no message or channel post")
            return

        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else None
        is_private = message.chat.type == 'private'
        is_group_or_channel = message.chat.type in ['group', 'supergroup', 'channel']

        # Handle /index link or forwarded message in private chat
        if is_private and user_id in self.indexing_requests:
            link_match = None
            if message.text:
                link_match = re.match(r'https://t\.me/c/(\d+)/(\d+)', message.text)
            forward_chat = message.forward_from_chat if (message.forward_from_chat and message.forward_from_chat.type in ['group', 'supergroup', 'channel']) else None

            if link_match:
                chat_id_from_link = f"-100{link_match.group(1)}"
                message_id = int(link_match.group(2))
                try:
                    bot_member = await context.bot.get_chat_member(chat_id_from_link, context.bot.id)
                    if bot_member.status == 'administrator':
                        self.indexing_requests[user_id]['chat_id'] = chat_id_from_link
                        self.logger.info(f"User {user_id} provided link for chat {chat_id_from_link}, message {message_id}")
                        await context.bot.send_message(user_id, f"Starting indexing for chat {chat_id_from_link}, message {message_id}...")
                        await self.index_file_from_link(context, chat_id_from_link, user_id, message_id)
                        await self.index_previous_files(context, chat_id_from_link, user_id, message_id - 1)
                        await context.bot.send_message(user_id, f"Finished indexing files for chat {chat_id_from_link}.")
                        del self.indexing_requests[user_id]
                    else:
                        self.logger.warning(f"Bot is not admin in chat {chat_id_from_link}")
                        await update.message.reply_text("I'm not an admin in that chat. Send another link or cancel with /cancel.")
                except telegram.error.Forbidden as e:
                    self.logger.error(f"Cannot access chat {chat_id_from_link}: {str(e)}")
                    await update.message.reply_text("I cannot access that chat. Send another link or cancel with /cancel.")
            elif forward_chat:
                try:
                    bot_member = await context.bot.get_chat_member(forward_chat.id, context.bot.id)
                    if bot_member.status == 'administrator':
                        self.indexing_requests[user_id]['chat_id'] = forward_chat.id
                        self.logger.info(f"User {user_id} started indexing for chat {forward_chat.id}")
                        if message.document or message.video or message.audio or message.photo:
                            file_id = self.get_file_id(message)
                            if file_id and file_id not in self.indexing_requests[user_id]['processed_ids']:
                                await self.index_file(message, context, forward_chat.id, is_forwarded=True, user_id=user_id)
                                self.indexing_requests[user_id]['processed_ids'].add(file_id)
                            message_id = message.forward_from_message_id if message.forward_from_message_id else message.message_id
                            await context.bot.send_message(user_id, f"Starting indexing for chat {forward_chat.id}, message {message_id}...")
                            await self.index_previous_files(context, forward_chat.id, user_id, message_id - 1)
                        else:
                            await update.message.reply_text("Please forward a message containing a file (document, video, audio, or photo).")
                            await self.index_previous_files(context, forward_chat.id, user_id, message.message_id - 1)
                        await context.bot.send_message(user_id, f"Finished indexing files for chat {forward_chat.id}.")
                        del self.indexing_requests[user_id]
                    else:
                        self.logger.warning(f"Bot is not admin in forwarded chat {forward_chat.id}")
                        await update.message.reply_text("I'm not an admin in that chat. Send another link or cancel with /cancel.")
                except telegram.error.Forbidden as e:
                    self.logger.error(f"Cannot access forwarded chat {forward_chat.id}: {str(e)}")
                    await update.message.reply_text("I cannot access that chat. Send another link or cancel with /cancel.")
            else:
                await update.message.reply_text("Please send a valid Telegram file link (e.g., https://t.me/c/1814841940/588956) or forward a file message.")
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

    async def cancel_command(self, update, context):
        user_id = update.message.from_user.id
        if user_id in self.indexing_requests:
            chat_id = self.indexing_requests[user_id]['chat_id']
            self.logger.info(f"User {user_id} cancelled indexing for chat {chat_id or 'unknown'}")
            await update.message.reply_text(f"Indexing cancelled for chat {chat_id or 'unknown'}.")
            del self.indexing_requests[user_id]
        else:
            self.logger.debug(f"/cancel command from user {user_id} with no active indexing")
            await update.message.reply_text("No active indexing session.")

    def get_file_id(self, message):
        if message.document:
            return message.document.file_id
        elif message.video:
            return message.video.file_id
        elif message.audio:
            return message.audio.file_id
        elif message.photo:
            return message.photo[-1].file_id
        return None

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
                    'timestamp': message.date.replace(tzinfo=UTC) if message.date.tzinfo else message.date,
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
                    'timestamp': message.date.replace(tzinfo=UTC) if message.date.tzinfo else message.date,
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
                    'timestamp': message.date.replace(tzinfo=UTC) if message.date.tzinfo else message.date,
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
                    'timestamp': message.date.replace(tzinfo=UTC) if message.date.tzinfo else message.date,
                    'forwarded': is_forwarded
                }

            if file_info:
                # For direct files, check for duplicates
                if not is_forwarded:
                    existing_file = self.db.files.find_one({'file_id': file_info['file_id']})
                    if existing_file:
                        self.logger.debug(f"Skipped duplicate file {file_info['name']} (ID: {file_info['file_id']}) in chat {target_chat_id}")
                        return

                # For forwarded files, check timestamp to ensure newer file
                if is_forwarded:
                    existing_file = self.db.files.find_one({'file_id': file_info['file_id']})
                    if existing_file:
                        # Normalize existing timestamp
                        existing_timestamp = existing_file['timestamp']
                        if isinstance(existing_timestamp, datetime) and existing_timestamp.tzinfo:
                            existing_timestamp = existing_timestamp.astimezone(UTC)
                        elif isinstance(existing_timestamp, datetime):
                            existing_timestamp = existing_timestamp.replace(tzinfo=UTC)
                        if existing_timestamp >= file_info['timestamp']:
                            self.logger.debug(f"Skipped older or same file {file_info['name']} (ID: {file_info['file_id']}) in chat {target_chat_id}")
                            return
                        self.db.files.delete_one({'file_id': file_info['file_id']})

                self.db.save_file(file_info)
                self.logger.info(f"Indexed file {file_info['name']} (ID: {file_info['file_id']}) in chat {target_chat_id}{' (forwarded)' if is_forwarded else ''}")
                if user_id:
                    await self.update_indexing_status(context, target_chat_id, user_id)
        except Exception as e:
            self.logger.error(f"Error indexing file in chat {target_chat_id}: {str(e)}")
            if user_id:
                await context.bot.send_message(user_id, f"Error indexing file in chat {target_chat_id}: {str(e)}")

    async def index_file_from_link(self, context, chat_id, user_id, message_id):
        try:
            api_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMessage"
            response = requests.get(api_url, params={'chat_id': chat_id, 'message_id': message_id})
            response.raise_for_status()
            data = response.json()

            if not data.get('ok') or 'result' not in data:
                self.logger.warning(f"No message found for ID {message_id} in chat {chat_id}: {data.get('description', 'Unknown error')}")
                await context.bot.send_message(user_id, f"No message found for ID {message_id} in chat {chat_id}. Try a different link.")
                return

            message_data = data['result']
            message = telegram.Message.de_json(message_data, context.bot)

            if message and (message.document or message.video or message.audio or message.photo):
                file_id = self.get_file_id(message)
                processed_ids = self.indexing_requests[user_id]['processed_ids']
                if file_id and file_id not in processed_ids:
                    await self.index_file(message, context, chat_id, is_forwarded=True, user_id=user_id)
                    processed_ids.add(file_id)
                else:
                    self.logger.debug(f"Skipped duplicate or non-media file {file_id or 'None'} in chat {chat_id}")
                    await context.bot.send_message(user_id, f"Message {message_id} in chat {chat_id} is a duplicate or does not contain a file.")
            else:
                self.logger.debug(f"Skipped non-media message {message_id} in chat {chat_id}")
                await context.bot.send_message(user_id, f"Message {message_id} in chat {chat_id} does not contain a file. Try a different link.")
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Error fetching message {message_id} in chat {chat_id}: {str(e)}")
            await context.bot.send_message(user_id, f"Error fetching message {message_id} in chat {chat_id}: {str(e)}. Try a different link.")
        except Exception as e:
            self.logger.error(f"Error indexing file from link for message {message_id} in chat {chat_id}: {str(e)}")
            await context.bot.send_message(user_id, f"Error indexing file from link for message {message_id} in chat {chat_id}: {str(e)}. Try a different link.")

    async def index_previous_files(self, context, chat_id, user_id, start_message_id):
        try:
            message_id = start_message_id
            messages_processed = 0
            max_messages = 100  # Limit to avoid rate limits
            consecutive_failures = 0
            max_consecutive_failures = 50  # Increased to allow more attempts
            processed_ids = self.indexing_requests[user_id]['processed_ids']
            api_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMessage"

            while messages_processed < max_messages and message_id > 0 and consecutive_failures < max_consecutive_failures:
                try:
                    response = requests.get(api_url, params={'chat_id': chat_id, 'message_id': message_id})
                    response.raise_for_status()
                    data = response.json()

                    if not data.get('ok') or 'result' not in data:
                        self.logger.debug(f"No message found for ID {message_id} in chat {chat_id}: {data.get('description', 'Unknown error')}")
                        consecutive_failures += 1
                        message_id -= 1
                        messages_processed += 1
                        continue

                    consecutive_failures = 0  # Reset on success
                    message_data = data['result']
                    message = telegram.Message.de_json(message_data, context.bot)

                    if message and (message.document or message.video or message.audio or message.photo):
                        file_id = self.get_file_id(message)
                        if file_id and file_id not in processed_ids:
                            await self.index_file(message, context, chat_id, is_forwarded=True, user_id=user_id)
                            processed_ids.add(file_id)
                        else:
                            self.logger.debug(f"Skipped duplicate or non-media file {file_id or 'None'} in chat {chat_id}")
                    else:
                        self.logger.debug(f"Skipped non-media message {message_id} in chat {chat_id}")

                    message_id -= 1
                    messages_processed += 1
                    await asyncio.sleep(0.3)  # Delay to avoid rate limits
                except requests.exceptions.RequestException as e:
                    self.logger.warning(f"Error fetching message {message_id} in chat {chat_id}: {str(e)}")
                    consecutive_failures += 1
                    message_id -= 1
                    messages_processed += 1
                    await asyncio.sleep(0.5)  # Extra delay on error
                except Exception as e:
                    self.logger.error(f"Error processing message {message_id} in chat {chat_id}: {str(e)}")
                    break

            self.logger.info(f"Completed indexing {messages_processed} messages for chat {chat_id} with {consecutive_failures} consecutive failures")
            await context.bot.send_message(user_id, f"Finished indexing files for chat {chat_id}. Processed {messages_processed} messages.")
        except Exception as e:
            self.logger.error(f"Error indexing previous files for chat {chat_id}: {str(e)}")
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
            self.app.add_handler(CommandHandler("cancel", self.cancel_command))
            self.app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_message))
            self.app.add_error_handler(self.error_handler)
            self.app.run_polling()
        except Exception as e:
            self.logger.error(f"Error running bot: {str(e)}")

if __name__ == "__main__":
    bot = TelegramBot()
    bot.run()
