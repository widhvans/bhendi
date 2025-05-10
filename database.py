from pymongo import MongoClient
import config

class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGO_URI)
        self.db = self.client[config.DATABASE_NAME]
        self.files = self.db.files
        self.files.create_index([("file_id", 1)], unique=True)
        self.files.create_index([("name", "text")])

    def save_file(self, file_info):
        try:
            self.files.insert_one(file_info)
            return True
        except:
            return False

    def file_exists(self, file_id):
        return self.files.find_one({"file_id": file_id}) is not None

    def search_files(self, query):
        return list(self.files.find({"$text": {"$search": query}}))

    def get_file_count(self, chat_id):
        return self.files.count_documents({"chat_id": chat_id})

    def close(self):
        self.client.close()
