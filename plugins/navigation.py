from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from pyrogram.errors import UserNotParticipant
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import get_session, is_user_banned

FORCE_CHANNEL_ID = -1002657096509

from plugins.subscription import check_force_sub

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if await is_user_banned(message.from_user.id):
        await message.reply_text("🚫 **Access Denied**\n\nYou are restricted from using this bot.")
        return

    # Unified Force Sub Check
    if not await check_force_sub(client, message):
        return

    user = message.from_user
    # personalized greeting
    mention = user.mention
    
    logged_in = bool(await get_session(user.id))
    
    text = (
        f"👋 **Hello {mention}, Welcome to ExtractX!**\n\n"
        "I am your advanced assistant for managing and extracting content from private Telegram channels.\n\n"
        "✨ **What can I do?**\n"
        "• 🔐 **Secure Login**: Use your own account safely.\n"
        "• 📥 **Batch Extraction**: Copy thousands of messages easily.\n"
        "• 🛠 **Power Tools**: Filter, Edit Captions, and Multi-Forward.\n\n"
        "🚀 **Get Started** by connecting your account or managing settings."
    )
    
    # Dynamic Buttons
    buttons = []
    if not logged_in:
        buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="login_flow")])
    else:
        buttons.append([InlineKeyboardButton("🚀 Start Batch Job", callback_data="start_batch")])
        
    buttons.append([
        InlineKeyboardButton("⚙️ Settings", callback_data="settings_flow"),
        InlineKeyboardButton("ℹ️ Help & Guide", callback_data="help_menu")
    ])
    
    buttons.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_start")])
    buttons.append([InlineKeyboardButton("📢 Join Official Channel", url="https://t.me/Univora88")])

    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@Client.on_message(filters.command("help") & filters.private)
async def help_command(client, message):
    await show_help_menu(client, message)

async def show_help_menu(client, message_or_callback):
    text = (
        "📚 **ExtractX User Guide**\n\n"
        "**1️⃣ Account Setup**\n"
        "• Click **Connect Account** or use `/login`.\n"
        "• Enter your phone number and OTP to authorize.\n"
        "• Your session is stored securely locally.\n\n"
        "**2️⃣ Destination Setup**\n"
        "• Go to **Settings** > **Channel Manager**.\n"
        "• Add the channels where you want files to be copied.\n"
        "• Make sure your connected account is an Admin there!\n\n"
        "**3️⃣ Starting a Job**\n"
        "• Use `/batch` or click **Start Batch Job**.\n"
        "• Send the private link of the **First Message**.\n"
        "• Choose how many messages to copy (or 'all').\n\n"
        "**4️⃣ Live Batch (Premium)**\n"
        "• Use `/livebatch` for real-time auto-forwarding.\n"
        "• Set source → destination mapping.\n"
        "• Bot monitors source and auto-forwards new messages!\n"
        "• Each source channel needs its own destination.\n"
        "• Limits: Free=0, Daily=2, Monthly=5, Ultra=15.\n\n"
        "**5️⃣ Advanced Features**\n"
        "• **Filters**: Choose to copy only Videos, Photos, etc.\n"
        "• **Captions**: Remove unwanted words or add your own credit."
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Home", callback_data="back_home")]
    ])
    
    if hasattr(message_or_callback, "message"): # Is Callback
        await message_or_callback.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_callback.reply_text(text, reply_markup=kb)

# Callback Handlers for Navigation
@Client.on_callback_query(filters.regex("^(login_flow|start_batch|settings_flow|help_menu|refresh_start|back_home)"))
async def nav_handler(client, callback):
    data = callback.data
    
    if data == "login_flow":
        await callback.answer()
        # Trigger Login Command Logic Manually
        # Import login handler? Or just instruct user.
        # Better: Simulate command or guide.
        await callback.message.reply_text("🔹 **To Login:**\n\nSend `/login` to start the process.")
        
    elif data == "start_batch":
        await callback.answer()
        await callback.message.reply_text("🔹 **To Start Batching:**\n\nSend `/batch` to begin extraction.")

    elif data == "settings_flow":
        await callback.answer()
        # Import settings handler function to reuse logic?
        # We can just ask user to type command or trigger it if we refactor.
        await callback.message.reply_text("🔹 **Settings:**\n\nSend `/settings` to open the panel.")

    elif data == "help_menu":
        await show_help_menu(client, callback)
        
    elif data == "refresh_start":
        # Re-render start
        from plugins.navigation import start_command # Recursive? 
        # Actually just re-call the logic. Since start_command takes message, we need to adapt.
        # Simplified: just delete and send new or edit.
        await callback.message.delete()
        # We can't easily recall the handler without message obj. 
        # But we can edit text to "Refreshed" then show content.
        # Let's just send the start text again.
        user = callback.from_user
        logged_in = bool(await get_session(user.id))
        
        text = (
            f"👋 **Hello {user.mention}, Welcome to ExtractX!**\n\n"
            "I am your advanced assistant for managing and extracting content from private Telegram channels.\n\n"
            "✨ **What can I do?**\n"
            "• 🔐 **Secure Login**: Use your own account safely.\n"
            "• 📥 **Batch Extraction**: Copy thousands of messages easily.\n"
            "• 🛠 **Power Tools**: Filter, Edit Captions, and Multi-Forward.\n\n"
            "🚀 **Get Started** by connecting your account or managing settings."
        )
        buttons = []
        if not logged_in:
            buttons.append([InlineKeyboardButton("🔐 Connect Account", callback_data="login_flow")])
        else:
            buttons.append([InlineKeyboardButton("🚀 Start Batch Job", callback_data="start_batch")])
            
        buttons.append([
            InlineKeyboardButton("⚙️ Settings", callback_data="settings_flow"),
            InlineKeyboardButton("ℹ️ Help & Guide", callback_data="help_menu")
        ])
        buttons.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_start")])
        
        await callback.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "back_home":
        # Delete help msg and show start
        await callback.message.delete()
        # Same logic as refresh
        await callback.message.reply_text("👋 **Welcome Back!**\n(Use /start for full menu)")
@Client.on_message(filters.command(["checkcommand", "commands"]) & filters.private)
async def command_list(client, message):
    user_id = message.from_user.id
    is_admin = (user_id == int(OWNER_ID))
    
    # Header
    text = "📂 **EXTRACT X COMMAND CENTER** 📂\n\n"
    
    # 👤 User Section
    text += "👤 **USER COMMANDS**\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    text += "• `/start` - 🏠 **Home Dashboard**: Initialize the bot & see status.\n"
    text += "• `/login` - 🔐 **Connect Account**: Login securely via Phone Number.\n"
    text += "• `/logout` - 👋 **Disconnect**: Remove your session safely.\n"
    text += "• `/batch` - 🚀 **Start Job**: Begin copying files from channels.\n"
    text += "• `/livebatch` - 📡 **Live Monitor**: Real-time auto-forwarding (Premium).\n"
    text += "• `/cancel` - 🛑 **Stop Job**: Immediately halt any running task.\n"
    text += "• `/settings` - ⚙️ **Config**: Manage channels, filters & captions.\n"
    text += "• `/showplan` - 💎 **My Plan**: Check subscription limits & expiry.\n"
    text += "• `/help` - ℹ️ **Guide**: How to use the bot effectively.\n"
    text += "• `/id` - 🆔 **Get ID**: Reply to media/forward to get IDs.\n"
    text += "\n"
    
    # 🛠 Admin Section (Only for Owner)
    if is_admin:
        text += "🛠 **ADMIN COMMANDS (God Mode)**\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += "• `/addpremium [ID] [Plan]` - 🎁 **Give Premium**: `day_19`, `month_199`, `unlimited_299`\n"
        text += "• `/removepremium [ID]` - 🔻 **Revoke**: Downgrade user to Free plan.\n"
        text += "• `/protect_channel [add/remove/list] [ID]` - 🛡️ **Protect Channels**: Prevent extraction.\n"
        text += "• `/stats` - 📊 **Statistics**: View bot usage & user counts.\n"
        text += "• `/ban [ID] [Reason]` - 🔨 **Ban User**: Block user from bot.\n"
        text += "• `/unban [ID]` - 🕊 **Unban**: Restore user access.\n"
        text += "• `/broadcast [Message]` - 📢 **Broadcast**: Send message to all users.\n"
        text += "\n"
        
    text += "💡 *Tap on any command to run it immediately.*"
    
    await message.reply_text(text)

@Client.on_message(filters.command("about") & filters.private)
async def about_command(client, message):
    text = (
        "🤖 **ABOUT EXTRACT X** 🤖\n\n"
        "**Access Restricted Content with Ease.**\n"
        "ExtractX is an advanced tool designed to securely copy and manage content from private Telegram channels where forwarding is restricted.\n\n"
        "🌟 **Key Features:**\n"
        "• ⚡ **High Speed:** Optimized for bulk processing.\n"
        "• 🔐 **Secure:** No data leaks, purely user-session based.\n"
        "• ☁️ **Cloud Native:** Running on high-performance servers.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👨‍💻 **Developer:** `Rolex Sir`\n"
        "🎓 *A passionate 10th Grade Student exploring the world of AI & Coding.*\n\n"
        "🏢 **Powered By:** `Univora`\n"
        "🚀 *Innovating for the future.*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📢 *\"I built this for fun and learning. Enjoy using it!\"*"
    )
    
    link = "https://t.me/univora"
    try:
        chat = await client.get_chat(FORCE_CHANNEL_ID)
        link = chat.invite_link or await client.export_chat_invite_link(FORCE_CHANNEL_ID)
    except:
        pass
        
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Visit Univora", url="https://univora.site")],
            [InlineKeyboardButton("📢 Join Official Channel", url=link)]
        ])
    )
