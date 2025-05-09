from pymongo import MongoClient
import config

class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGO_URI)
        self.db = self.client['telegram_bot']
        self.files = self.db['files']
        
        # Create index for faster searches
        self.files.create_index([('chat_id', 1), ('file_name', 'text')])
    
    def save_file(self, chat_id, file_name, file_id):
        self.files.update_one(
            {
                'chat_id': chat_id,
                'file_name': file_name,
                'file_id': file_id
            },
            {
                '$set': {
                    'chat_id': chat_id,
                    'file_name': file_name,
                    'file_id': file_id
                }
            },
            upsert=True
        )
    
    def search_files(self, chat_id, search_term):
        return list(self.files.find({
            'chat_id': chat_id,
            '$text': {'$search': search_term}
        }))
    
    def close(self):
        self.client.close()
