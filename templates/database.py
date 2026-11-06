import os
import json
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI")
DB_FILE = os.getenv("DB_FILE", "bank_db.json")

mongo_collection = None
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = client["dekontika_bank"]
        mongo_collection = db["system_state"]
        client.admin.command('ping')
        print("MongoDB Atlas baglantisi basarili.")
    except Exception as exc:
        print(f"MongoDB baglanti hatasi, yerel JSON'a donuluyor: {exc}")
        mongo_collection = None

def mongo_load():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main_db"})
            if doc:
                doc.pop("_id", None)
                return doc
        except Exception as e:
            print(f"Mongo okuma hatasi: {e}")
    return None

def mongo_save(data):
    if mongo_collection is not None:
        try:
            payload = dict(data)
            payload.pop("_id", None)
            mongo_collection.update_one({"_id": "main_db"}, {"$set": payload}, upsert=True)
            return True
        except Exception as e:
            print(f"Mongo yazma hatasi: {e}")
    return False
