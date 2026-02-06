import os
from dotenv import load_dotenv

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN") # For the bot interface
SESSION_STRING = os.getenv("SESSION_STRING") # For the user client
OWNER_ID = int(os.getenv("OWNER_ID", "0")) # To restrict usage to the owner
MONGO_URI = os.getenv("MONGO_URI")
FORCE_CHANNEL_ID = -1002657096509
