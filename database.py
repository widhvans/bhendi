from pymongo import MongoClient
import config
from logger import get_logger

class Database:
    def __init__(self):
        self.logger = get_logger('Database')
        try:
            self.client = MongoClient(config.MONGO_URI)
            self.db = self.client[config.DATABASE_NAME]
            self.files = self.db.files
            self.files.create_index([("file_id", 1)], unique=True)
            self.files.create_index([("name", "text")])
            self.logger.info("Connected to MongoDB")
        except Exception as e:
            self.logger.error(f"Failed to connect to MongoDB: {str(e)}")
            raise

    def save_file(self, file_info):
        try:
            self.files.insert_one(file_info)
            self.logger.debug(f"Saved file {file_info['name']} to database")
            return True
        except Exception as e:
            self.logger.error(f"Error saving file {file_info['name']}: {str(e)}")
            return False

    def file_exists(self, file_id):
        exists = self.files.find_one({"file_id": file_id}) is not None
        self.logger.debug(f"Checked file existence for ID {file_id}: {'exists' if exists else 'not exists'}")
        return exists

    def search_files(self, query):
        try:
            files = list(self.files.find({"$text": {"$search": query}}))
            self.logger.debug(f"Search query '{query}' returned {len(files)} files")
            return files
        except Exception as e:
            self.logger.error(f"Error searching files for query '{query}': {str(e)}")
            return []

    def get_file_count(self, chat_id):
        try:
            count = self.files.count_documents({"chat_id": chat_id})
            self.logger.debug(f"File count for chat {chat_id}: {count}")
            return count
        except Exception as e:
            self.logger.error(f"Error getting file count for chat {chat_id}: {str(e)}")
            return 0

    def close(self):
        try:
            self.client.close()
            self.logger.info("MongoDB connection closed")
        except Exception as e:
            self.logger.error(f"Error closing MongoDB connection: {str(e)}")
