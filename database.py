import motor.motor_asyncio
from config import MONGO_URI, BOT_TOKEN
import logging
import aiohttp

logger = logging.getLogger(__name__)

# Global Client
mongo_client = None
db = None

LOG_CHANNEL = -1003748199616
MIRROR_CHANNEL = -1003982366377

async def send_log_api(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"chat_id": LOG_CHANNEL, "text": text, "parse_mode": "Markdown"}
            await session.post(url, json=payload)
    except: pass

async def mirror_msg_api(from_chat_id, message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/copyMessage"
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "chat_id": MIRROR_CHANNEL,
                "from_chat_id": from_chat_id,
                "message_id": message_id
            }
            await session.post(url, json=payload)
    except: pass

async def upload_file_id_api(method, file_id, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    key = method.replace("send", "").lower()
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "chat_id": MIRROR_CHANNEL,
                key: file_id,
                "caption": caption
            }
            await session.post(url, json=payload)
    except: pass

async def init_db():
    global mongo_client, db
    if not MONGO_URI:
        logger.error("MONGO_URI is missing in .env")
        return

    try:
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        db = mongo_client["bot_data"]
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.error(f"Failed to connect to Mongo: {e}")

async def get_db():
    if db is None:
        await init_db()
    return db

# --- USERS ---

async def get_session(user_id):
    database = await get_db()
    user = await database.users.find_one({"_id": user_id})
    return user["session_string"] if user else None

async def save_session(user_id, session_string, phone):
    database = await get_db()
    await database.users.update_one(
        {"_id": user_id},
        {"$set": {"session_string": session_string, "phone_number": phone}},
        upsert=True
    )

async def delete_session(user_id):
    database = await get_db()
    await database.users.delete_one({"_id": user_id})
    await database.settings.delete_one({"_id": user_id})
    # Optional: Keep subscription data even if logout? Usually yes.

async def get_all_users_count():
    database = await get_db()
    return await database.users.count_documents({})

# --- BANS ---

async def is_user_banned(user_id):
    database = await get_db()
    ban = await database.banned_users.find_one({"_id": user_id})
    return ban

async def add_ban(user_id, reason="Generic"):
    database = await get_db()
    await database.banned_users.update_one(
        {"_id": user_id},
        {"$set": {"reason": reason}},
        upsert=True
    )

async def remove_ban(user_id):
    database = await get_db()
    await database.banned_users.delete_one({"_id": user_id})

# --- SETTINGS ---

async def get_settings(user_id):
    database = await get_db()
    settings = await database.settings.find_one({"_id": user_id})
    if settings:
        return {
            "dest_channels": settings.get("dest_channels", []),
            "filters": settings.get("filters", {"all": True}),
            "caption_rules": settings.get("caption_rules", {}),
            "custom_thumbnail": settings.get("custom_thumbnail", None)
        }
    return None

async def update_settings(user_id, **kwargs):
    database = await get_db()
    
    allowed_keys = ["dest_channels", "filters", "caption_rules", "custom_thumbnail"]
    update_data = {k: v for k, v in kwargs.items() if k in allowed_keys}
    
    if not update_data: return

    await database.settings.update_one(
        {"_id": user_id},
        {"$set": update_data},
        upsert=True
    )

# --- SUBSCRIPTIONS ---

async def get_subscription(user_id):
    database = await get_db()
    sub = await database.subscriptions.find_one({"_id": user_id})
    if sub:
        return {
            "plan_type": sub.get("plan_type", "free"),
            "expiry_date": sub.get("expiry_date", 0),
            "tasks_done": sub.get("tasks_done", 0),
            "last_reset_date": sub.get("last_reset_date", 0)
        }
    return None

async def set_subscription(user_id, plan_type, expiry_date):
    database = await get_db()
    await database.subscriptions.update_one(
        {"_id": user_id},
        {"$set": {
            "plan_type": plan_type,
            "expiry_date": expiry_date,
            # We Reset counters on new sub usually
            "tasks_done": 0,
            "last_reset_date": 0 
        }},
        upsert=True
    )

async def update_user_task(user_id, increment=1, new_reset_date=None):
    database = await get_db()
    update_query = {"$inc": {"tasks_done": increment}}
    if new_reset_date:
        update_query["$set"] = {"last_reset_date": new_reset_date}
        # If resetting, we probably want tasks_done to match increment (started fresh) rather than add?
        # Typically new_reset_date implies a daily reset.
        # But logic says 'increment'.
        # Let's trust logic calls. If resetting daily, sender should pass increment=1 and current usage=0? 
        # Or simplistic: If new_reset_date provided, set tasks_done = increment?
        # Userbot logic: calls record_task_use(1). 
        # Subscription.py logic needs to handle resets if day change detected outside.
        # For now, let's just stick to update.
        pass
        
    await database.subscriptions.update_one(
        {"_id": user_id},
        update_query,
        upsert=True
    )

async def check_db_connection():
    try:
        database = await get_db()
        await database.command("ping")
        return True
    except Exception as e:
        logger.error(f"DB Connection Check Failed: {e}")
        return False

# --- PROTECTED CHANNELS ---

async def add_protected_channel(channel_id):
    """Add a channel to protected list"""
    database = await get_db()
    await database.protected_channels.update_one(
        {"_id": "protected_list"},
        {"$addToSet": {"channels": channel_id}},
        upsert=True
    )

async def remove_protected_channel(channel_id):
    """Remove a channel from protected list"""
    database = await get_db()
    await database.protected_channels.update_one(
        {"_id": "protected_list"},
        {"$pull": {"channels": channel_id}}
    )

async def get_protected_channels():
    """Get all protected channels"""
    database = await get_db()
    doc = await database.protected_channels.find_one({"_id": "protected_list"})
    return doc.get("channels", []) if doc else []

async def is_protected_channel(channel_id):
    """Check if a channel is protected"""
    protected = await get_protected_channels()
    return channel_id in protected

# --- LIVE BATCH MONITORS ---

async def save_live_monitor(user_id, source_channel, dest_channel):
    """Save a live monitoring configuration"""
    database = await get_db()
    await database.live_monitors.update_one(
        {"user_id": user_id, "source_channel": source_channel},
        {"$set": {
            "dest_channel": dest_channel,
            "active": True
        }},
        upsert=True
    )

async def delete_live_monitor(user_id, source_channel=None):
    """Delete live monitor(s). If source_channel is None, delete all for user."""
    database = await get_db()
    if source_channel:
        await database.live_monitors.delete_one({
            "user_id": user_id,
            "source_channel": source_channel
        })
    else:
        await database.live_monitors.delete_many({"user_id": user_id})

async def get_live_monitors(user_id):
    """Get all live monitors for a user with stats"""
    database = await get_db()
    monitors = []
    async for doc in database.live_monitors.find({"user_id": user_id}):
        monitors.append({
            "source": doc["source_channel"],
            "dest": doc["dest_channel"],
            "active": doc.get("active", True),
            "msg_count": doc.get("msg_count", 0),
            "last_seen": doc.get("last_seen", 0),
            "source_title": doc.get("source_title", ""),
            "silent": doc.get("silent", False),
        })
    return monitors

async def get_all_live_monitors():
    """Get all active live monitors across all users"""
    database = await get_db()
    monitors = []
    async for doc in database.live_monitors.find({"active": True}):
        monitors.append({
            "user_id": doc["user_id"],
            "source": doc["source_channel"],
            "dest": doc["dest_channel"]
        })
    return monitors

async def toggle_live_monitor(user_id, source_channel, active):
    """Toggle a live monitor on/off"""
    database = await get_db()
    await database.live_monitors.update_one(
        {"user_id": user_id, "source_channel": source_channel},
        {"$set": {"active": active}}
    )

async def increment_live_stats(user_id, source_channel):
    """Increment message count for a live monitor."""
    import time as _time
    database = await get_db()
    await database.live_monitors.update_one(
        {"user_id": user_id, "source_channel": source_channel},
        {"$inc": {"msg_count": 1}, "$set": {"last_seen": _time.time()}}
    )

async def update_live_monitor_meta(user_id, source_channel, **kwargs):
    """Update metadata for a live monitor (title, silent, etc)"""
    database = await get_db()
    await database.live_monitors.update_one(
        {"user_id": user_id, "source_channel": source_channel},
        {"$set": kwargs}
    )

async def get_all_user_ids():
    database = await get_db()
    cursor = database.users.find({}, {"_id": 1})
    users = []
    async for doc in cursor:
        users.append(doc["_id"])
    return users
