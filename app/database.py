from pymongo import ASCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.config import get_settings

settings = get_settings()
client = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
db: Database = client[settings.mongo_db_name]

users = db["users"]
trades = db["trades"]
support_tickets = db["support_tickets"]
referrals = db["referrals"]
wallet_logs = db["wallet_logs"]
otps = db["otps"]
broker_connections = db["broker_connections"]
risk_profiles = db["risk_profiles"]
trade_events = db["trade_events"]
master_broker = db["master_broker"]


def setup_indexes() -> None:
    indexes = (
        (users, [("email", ASCENDING)], {"unique": True}),
        (users, [("client_id", ASCENDING)], {"unique": True}),
        (users, [("mobile", ASCENDING)], {"unique": True}),
        (users, [("referral_code", ASCENDING)], {"unique": True}),
        (support_tickets, [("complaint_id", ASCENDING)], {"unique": True}),
        (support_tickets, [("user_id", ASCENDING), ("updated_at", ASCENDING)], {}),
        (trades, [("trade_id", ASCENDING)], {"unique": True}),
        (trades, [("user_id", ASCENDING), ("created_at", ASCENDING)], {}),
        (referrals, [("referrer_user_id", ASCENDING), ("created_at", ASCENDING)], {}),
        (otps, [("user_id", ASCENDING), ("purpose", ASCENDING)], {}),
        (broker_connections, [("user_id", ASCENDING)], {"unique": True}),
        (risk_profiles, [("user_id", ASCENDING)], {"unique": True}),
        (trade_events, [("trade_id", ASCENDING), ("created_at", ASCENDING)], {}),
        (master_broker, [("broker_name", ASCENDING)], {"unique": True}),
    )
    try:
        for collection, keys, options in indexes:
            collection.create_index(keys, **options)
    except PyMongoError as exc:
        print(f"MongoDB index setup skipped: {exc}")


def check_connection() -> bool:
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False
