from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from pyrogram.errors import UserNotParticipant
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID
from database import get_session, is_user_banned, send_log_api

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
    
    # Send Silent Log
    try:
        if "start" in message.text:
            log_text = (
                "🆕 **USER STARTED BOT**\n\n"
                f"👤 **Name:** [{user.first_name}](tg://user?id={user.id})\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"🔗 **Username:** {f'@{user.username}' if user.username else 'None'}"
            )
            await send_log_api(log_text)
    except: pass
    
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

async def show_help_menu(client, message_or_callback, page=1):
    user_id = message_or_callback.from_user.id
    is_admin = (user_id == int(OWNER_ID))
    
    if page == 1:
        text = (
            "📝 **ExtractX Hub - User Guide (1/2)**\n\n"
            "**1. Core Extraction 🚀**\n"
            "• `/batch` - 📦 Bulk Extraction (Extract thousands of posts at once)\n"
            "• `/livebatch` - 📡 Real-time Auto-Forwarding monitor\n"
            "• `/cancel` - 🛑 Stop ongoing extraction processes instantly\n\n"
            "**2. Account & Security 🔐**\n"
            "• `/login` - 🔑 Connect account via phone (Required for Private channels)\n"
            "• `/logout` - 👋 Wipe out session data safely\n"
            "• `/id` - 🆔 Get IDs of forwarded messages or channels\n\n"
            "**3. Configuration Workspace ⚙️**\n"
            "• `/settings` - 🛠 Open the **Control Center** to configure:\n"
            "   └ 🏷 **Custom Captions** (Add, Remove, Prefix, Suffix)\n"
            "   └ 📂 **Channel Manager** (Set multiple destinations)\n"
            "   └ 🖼 **Thumbnail Editor** (Set custom photo for videos/docs)\n"
            "   └ 🔍 **Filters** (Extract photos/videos/audio only)\n\n"
            "🚀 *Tap the Next button for Page 2 & Advanced details!*"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Next Page ➡️", callback_data="help_pg_2")],
            [InlineKeyboardButton("🔙 Back to Home", callback_data="back_home"), InlineKeyboardButton("❌ Close", callback_data="close_help")]
        ])
    else:
        text = (
            "📝 **ExtractX Hub - Advanced Guide (2/2)**\n\n"
            "**4. Metrics & Subscriptions 💎**\n"
            "• `/showplan` - 📜 View your active plan, counters, and limits\n"
            "• `/about` - 🤖 Details about the dev and platform\n"
        )
        
        if is_admin:
            text += (
                "\n**5. Admin Overrides (God Mode) 🛠**\n"
                "• `/status` or `/stats` - 📊 Server stats & active user counts\n"
                "• `/addpremium [ID] [Plan]` - 🎁 Give `day`, `month`, or `unlimited`\n"
                "• `/removepremium [ID]` - 🔻 Revoke Premium access\n"
                "• `/protect_channel add [ID]` - 🛡 Lock specific channels\n"
                "• `/ban [ID] [Reason]` - 🔨 Block abusers globally\n"
                "• `/broadcast [Message]` - 📢 Send alert to all users\n"
            )
            
        text += "\n\n💡 *Tip: Sending any command while waiting for an input will smartly auto-cancel the input!*"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Previous Page", callback_data="help_pg_1")],
            [InlineKeyboardButton("🔙 Back to Home", callback_data="back_home"), InlineKeyboardButton("❌ Close", callback_data="close_help")]
        ])

    is_callback = hasattr(message_or_callback, "message")
    try:
        if is_callback:
            await message_or_callback.message.edit_text(text, reply_markup=kb)
        else:
            await message_or_callback.reply_text(text, reply_markup=kb)
    except Exception:
        pass # Handle cases where text doesn't change

# Callback Handlers for Navigation
@Client.on_callback_query(filters.regex("^(login_flow|start_batch|settings_flow|help_menu|help_pg_1|help_pg_2|refresh_start|back_home|close_help)"))
async def nav_handler(client, callback):
    data = callback.data
    
    if data == "close_help":
        await callback.message.delete()
        
    elif data == "help_pg_1":
        await show_help_menu(client, callback, page=1)
        
    elif data == "help_pg_2":
        await show_help_menu(client, callback, page=2)
        
    elif data == "login_flow":
        await callback.answer()
        await callback.message.reply_text("🔹 **To Login:**\n\nSend `/login` to start the secure process.")
        
    elif data == "start_batch":
        await callback.answer()
        await callback.message.reply_text("🔹 **To Start Batching:**\n\nSend `/batch` to begin bulk extraction.")

    elif data == "settings_flow":
        await callback.answer()
        await callback.message.reply_text("🔹 **Settings:**\n\nSend `/settings` to open the panel.")

    elif data == "help_menu":
        await show_help_menu(client, callback, page=1)
        
    elif data == "refresh_start":
        await callback.message.delete()
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
        await callback.message.delete()
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
    text += "• `/showplan` - 💎 **My Plan**: Check limits, activate trial & upgrade.\n"
    text += "• `/myplan` - 📊 **Plan Status**: Quick plan & usage check.\n"
    text += "• `/help` - ℹ️ **Guide**: How to use the bot effectively.\n"
    text += "• `/id` - 🆔 **Get ID**: Reply to media/forward to get IDs.\n"
    text += "\n"
    
    # 🛠 Admin Section (Only for Owner)
    if is_admin:
        text += "🛠 **ADMIN COMMANDS (God Mode)**\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += "• `/addpremium [ID] [Plan]` - 🎁 **Give Premium**\n"
        text += "  Plans: `daily_39` | `monthly_259` | `ultra_389` | `lifetime_2999`\n"
        text += "• `/removepremium [ID]` - 🔻 **Revoke**: Downgrade user to Free plan.\n"
        text += "• `/givetrial [ID]` - 🎁 **Give Trial**: Grant/reset trial for user.\n"
        text += "• `/protect_channel [add/remove/list] [ID]` - 🛡️ **Protect Channels**.\n"
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
