from pymongo import MongoClient
import config

class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGO_URI)
        self.db = self.client['telegram_bot']
        self.files = self.db['files']
        self.index_state = self.db['index_state']
        
        # Create index for faster searches
        self.files.create_index([('chat_id', 1), ('file_name', 'text')])
        self.index_state.create_index([('chat_id', 1)])
    
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
    
    def get_file_count(self, chat_id):
        return self.files.count_documents({'chat_id': chat_id})
    
    def save_indexed_offset(self, chat_id, offset):
        self.index_state.update_one(
            {'chat_id': chat_id},
            {'$set': {'last_offset': offset}},
            upsert=True
        )
    
    def get_last_indexed_offset(self, chat_id):
        state = self.index_state.find_one({'chat_id': chat_id})
        return state.get('last_offset', 0) if state else 0
    
    def close(self):
        self.client.close()
