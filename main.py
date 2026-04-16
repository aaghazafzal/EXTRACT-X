import asyncio
import logging
import asyncio
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand
from config import API_ID, API_HASH, BOT_TOKEN
import os
from aiohttp import web
from database import init_db, update_settings, get_settings, is_user_banned, get_all_user_ids

# Plugins
from plugins.auth import handle_auth_input
from plugins.copy_manager import handle_batch_input
from plugins.settings import show_settings_panel
from plugins.livebatch import handle_livebatch_input, init_live_monitors

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Bot
bot = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=dict(root="plugins"),
    in_memory=True
)

# Text Handler for Inputs (Auth/Batch/Settings)
@bot.on_message(filters.text & filters.private, group=1)
async def input_handler(client, message):
    # Check Ban Status
    if await is_user_banned(message.from_user.id):
        await message.reply_text("🚫 **Access Denied**\n\nYou are restricted from using this bot.\nContact Admin for support.")
        return

    # User Command Interceptor (Cancel Pending States)
    if message.text and message.text.startswith("/"):
        canceled = False
        if hasattr(client, "waiting_channel_user") and client.waiting_channel_user == message.from_user.id:
            del client.waiting_channel_user
            canceled = True
        if hasattr(client, "waiting_input") and client.waiting_input.get("user") == message.from_user.id:
            del client.waiting_input
            canceled = True
        
        if canceled:
            await message.reply_text("🚫 **Action Cancelled** (New command detected).")
        return

    # Check Auth Input
    if await handle_auth_input(client, message):
        return

    # Check Batch Input
    if await handle_batch_input(client, message):
        return
    
    # Check Live Batch Input
    if await handle_livebatch_input(client, message):
        return
        
    # Check Settings Add Channel Input
    # We implement simple state here for "waiting_channel" if needed
    # Or handled via a separate mechanism.
    # For now, let's implement the Add Channel input logic here simply.
    # Check Settings Input (Channel Add, Captions)
    if hasattr(client, "waiting_channel_user") and client.waiting_channel_user == message.from_user.id:
        new_channel = None
        if message.forward_from_chat:
            new_channel = str(message.forward_from_chat.id)
        elif message.text:
            new_channel = message.text.strip()
        
        if not new_channel:
            await message.reply_text("⚠️ **Invalid Input**\nPlease send a valid Channel ID, Username, or forward a message from the channel.")
            return
        
        # Proper append logic
        s = await get_settings(message.from_user.id)
        
        if not s:
            s = {"dest_channels": [], "filters": {"all": True}}
            await update_settings(message.from_user.id)
            
        current = s.get("dest_channels", [])
        
        if new_channel not in current:
            current.append(new_channel)
            await update_settings(message.from_user.id, dest_channels=current)
            await message.reply_text(f"✅ Channel `{new_channel}` added.\nTotal: {len(current)}")
        else:
             await message.reply_text(f"⚠️ Channel `{new_channel}` already exists.")
             
        del client.waiting_channel_user
        await show_settings_panel(message.from_user.id, message, is_edit=False)
        return

    # Check Caption Inputs
    if hasattr(client, "waiting_input"):
        wait_data = client.waiting_input
        if wait_data.get("user") == message.from_user.id:
            itype = wait_data.get("type")
            text = message.text
            user_id = message.from_user.id
            
            settings = await get_settings(user_id)
            if not settings: 
                settings = {"dest_channels": [], "filters": {"all": True}, "caption_rules": {}}
                await update_settings(user_id)
            
            # Robust Rules Initialization
            rules = settings.get("caption_rules") or {}
            if "removals" not in rules: rules["removals"] = []
            if "replacements" not in rules: rules["replacements"] = {}
            if "prefix" not in rules: rules["prefix"] = ""
            if "suffix" not in rules: rules["suffix"] = ""

            if itype == "rem_word":
                rules["removals"].append(text)
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Added removal rule for: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "rep_word_old":
                client.waiting_input = {"user": user_id, "type": "rep_word_new", "old_word": text}
                await message.reply_text(f"➡️ Now send the **NEW Word** to replace `{text}` with:")
                
            elif itype == "rep_word_new":
                old = wait_data.get("old_word")
                rules["replacements"][old] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Added replacement: `{old}` -> `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "set_prefix":
                rules["prefix"] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Prefix set to: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "set_suffix":
                rules["suffix"] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Suffix set to: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)
            
            return

async def web_server():
    async def handle(request):
        return web.Response(text="ExtractX Bot is Online & Running!")
    
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web Server started on port {port}")

async def main():
    await init_db()
    
    # Start Web Server (For Render Port Binding)
    await web_server()

    print("Starting Bot...")
    await bot.start()
    
    # Set Bot Menu Commands
    await bot.set_bot_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("login", "🔐 Login Account"),
        BotCommand("logout", "👋 Logout"),
        BotCommand("settings", "⚙️ Configure"),
        BotCommand("batch", "🚀 Start Copying"),
        BotCommand("livebatch", "📡 Live Monitor"),
        BotCommand("cancel", "❌ Stop Job"),
        BotCommand("showplan", "💎 My Plan"),
        BotCommand("checkcommand", "📂 All Commands"),
        BotCommand("about", "🤖 About Bot"),
        BotCommand("help", "ℹ️ Guide")
    ])
    
    print("Bot Started! Commands Set.")
    
    # Initialize Live Monitors
    print("Initializing live monitors...")
    await init_live_monitors(bot)
    print("Live monitors ready!")
    
    # Startup Notification
    try:
        print("Sending Startup Notification...")
        users = await get_all_user_ids()
        count = 0
        from pyrogram.errors import FloodWait
        for uid in users:
            try:
                await bot.send_message(
                    uid,
                    "🚀 **System Restarted & Online**\n\n"
                    "ExtractX is now live and ready for processing.\n"
                    "Tap /start to manage your tasks.\n\n"
                    "⚡ _Powered by Univora_"
                )
                count += 1
                await asyncio.sleep(0.05)
            except FloodWait as e:
                print(f"FloodWait: Sleeping {e.value}s")
                await asyncio.sleep(e.value)
            except Exception:
                pass
        print(f"Notified {count} users.")
    except Exception as e:
        print(f"Startup Notify Error: {e}")

    await idle()
    await bot.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
