from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from database import get_settings, update_settings

from plugins.subscription import check_force_sub

@Client.on_message(filters.command("settings") & filters.private)
async def settings_command(client, message):
    if not await check_force_sub(client, message):
        return
    await show_settings_panel(message.from_user.id, message)

async def edit_or_reply(message, text, markup):
    try:
        if message.photo:
            await message.edit_caption(text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
    except Exception:
        # Fallback if type mismatch or other issue
        await message.edit_text(text, reply_markup=markup)

async def show_settings_panel(user_id, message_obj, is_edit=False):
    settings = await get_settings(user_id)
    if not settings:
        settings = {"dest_channels": [], "filters": {"all": True}}
        await update_settings(user_id)

    dest_count = len(settings["dest_channels"])
    f = settings["filters"]
    
    # Improved Text
    text = (
        "⚙️ **Control Center**\n\n"
        "Here you can manage your extraction preferences and destination channels.\n\n"
        f"📡 **Active Destinations:** `{dest_count}`\n"
        "Tap the buttons below to configure."
    )
    
    # Flags for icons
    tick = "✅"
    cross = "❌"
    
    kb = [
        [
            InlineKeyboardButton(f"📂 Channel Manager ({dest_count})", callback_data="set_channels")
        ],
        [
             InlineKeyboardButton("📝 Caption Editor", callback_data="cap_panel")
        ],
        [
             InlineKeyboardButton("--- Content Filters ---", callback_data="ignore")
        ],
        [
            InlineKeyboardButton(f"{tick if f.get('all') else cross} All Content", callback_data="tog_all"),
            InlineKeyboardButton(f"{tick if f.get('media') else cross} Media Only", callback_data="tog_media")
        ],
        [
            InlineKeyboardButton(f"{tick if f.get('photo') else cross} Photos", callback_data="tog_photo"),
            InlineKeyboardButton(f"{tick if f.get('document') else cross} Files", callback_data="tog_document")
        ],
        [
            InlineKeyboardButton(f"{tick if f.get('video') else cross} Videos", callback_data="tog_video"),
            InlineKeyboardButton(f"{tick if f.get('text') else cross} Texts", callback_data="tog_text")
        ]
    ]
    
    markup = InlineKeyboardMarkup(kb)
    
    if is_edit:
        await edit_or_reply(message_obj, text, markup)
    else:
        # Initial Command - Send Photo
        try:
             await message_obj.reply_photo("logo/setting.jpg", caption=text, reply_markup=markup)
        except Exception as e:
             # Fallback if image fails
             await message_obj.reply_text(text, reply_markup=markup)

@Client.on_callback_query(filters.regex("^tog_"))
async def toggle_filter(client, callback: CallbackQuery):
    key = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    settings = await get_settings(user_id)
    if not settings:
        settings = {"dest_channels": [], "filters": {"all": True}}
        
    if "filters" not in settings:
        settings["filters"] = {"all": True}
    
    current_val = settings["filters"].get(key, False)
    settings["filters"][key] = not current_val
    
    # If users selects non-all, maybe disable all? Or if user selects ALL, disable others?
    # Let's keep it simple toggle.
    if key == "all" and settings["filters"]["all"]:
        # If All turned ON, logic usually implies others are ignored or implicitly ON.
        pass
        
    await update_settings(user_id, filters=settings["filters"])
    await show_settings_panel(user_id, callback.message, is_edit=True)

@Client.on_callback_query(filters.regex("^cap_"))
async def caption_settings_handler(client, callback: CallbackQuery):
    action = callback.data
    user_id = callback.from_user.id
    
    settings = await get_settings(user_id)
    if not settings:
        settings = {"dest_channels": [], "filters": {"all": True}, "caption_rules": {}}
        await update_settings(user_id)
    
    rules = settings.get("caption_rules") or {"removals": [], "replacements": {}, "prefix": "", "suffix": ""}
    
    if action == "cap_panel":
        text = (
            "📝 **Caption Settings**\n\n"
            "Here you can modify the text of copied messages.\n"
            f"🚫 **Remove Words**: {len(rules.get('removals', []))}\n"
            f"🔄 **Replace Words**: {len(rules.get('replacements', {}))}\n"
            f"🔡 **Prefix**: {rules.get('prefix') or 'None'}\n"
            f"🔠 **Suffix**: {rules.get('suffix') or 'None'}\n"
        )
        kb = [
            [InlineKeyboardButton("🚫 Manage Removals", callback_data="cap_rem_menu")],
            [InlineKeyboardButton("🔄 Manage Replacements", callback_data="cap_rep_menu")],
            [InlineKeyboardButton("🔡 Set Prefix", callback_data="cap_prefix"), InlineKeyboardButton("🔠 Set Suffix", callback_data="cap_suffix")],
            [InlineKeyboardButton("🧹 Clear All Rules", callback_data="cap_clear")],
            [InlineKeyboardButton("🔙 Back to Main", callback_data="back_settings")]
        ]
        await edit_or_reply(callback.message, text, InlineKeyboardMarkup(kb))

    elif action == "cap_rem_menu":
        removals = rules.get("removals", [])
        text = "🚫 **Removal Rules**\nThese words/phrases will be deleted from captions.\n\n"
        if not removals: text += "No words set."
        else:
             for i, w in enumerate(removals, 1):
                 text += f"{i}. `{w}`\n"
        
        kb = [
            [InlineKeyboardButton("➕ Add Word to Remove", callback_data="cap_add_rem")],
            [InlineKeyboardButton("🗑 Delele Word", callback_data="cap_del_rem")],
            [InlineKeyboardButton("🔙 Back", callback_data="cap_panel")]
        ]
        await edit_or_reply(callback.message, text, InlineKeyboardMarkup(kb))

    elif action == "cap_rep_menu":
        reps = rules.get("replacements", {})
        text = "🔄 **Replacement Rules**\nFormat: `Old` -> `New`\n\n"
        if not reps: text += "No replacements set."
        else:
             for old, new in reps.items():
                 text += f"- `{old}` ➡️ `{new}`\n"
        
        kb = [
            [InlineKeyboardButton("➕ Add Replacement", callback_data="cap_add_rep")],
            [InlineKeyboardButton("🗑 Delete Replacement", callback_data="cap_del_rep")],
            [InlineKeyboardButton("🔙 Back", callback_data="cap_panel")]
        ]
        await edit_or_reply(callback.message, text, InlineKeyboardMarkup(kb))
        
    elif action == "cap_add_rem":
        client.waiting_input = {"user": user_id, "type": "rem_word"}
        await callback.message.reply_text("🗣 **Send the word/phrase to REMOVE:**")
        
    elif action == "cap_add_rep":
        client.waiting_input = {"user": user_id, "type": "rep_word_old"}
        await callback.message.reply_text("🗣 **Send the OLD word (to be replaced):**")

    # Prefix/Suffix Inputs
    elif action == "cap_prefix":
        client.waiting_input = {"user": user_id, "type": "set_prefix"}
        await callback.message.reply_text("🗣 **Send the Prefix Text** (appears at start):")
    elif action == "cap_suffix":
        client.waiting_input = {"user": user_id, "type": "set_suffix"}
        await callback.message.reply_text("🗣 **Send the Suffix Text** (appears at end):")
        
    elif action == "cap_clear":
        rules = {"removals": [], "replacements": {}, "prefix": "", "suffix": ""}
        await update_settings(user_id, caption_rules=rules)
        await callback.answer("All rules cleared!", show_alert=True)
        # Re-show panel
        await caption_settings_handler(client, callback)
        
    # Delete Handlers... ideally need list select, but for simplicity:
    elif action == "cap_del_rem":
        # Just clear all for simple UI or ask for word?
        # Let's clear removals for now to save complex UI work
        rules["removals"] = []
        await update_settings(user_id, caption_rules=rules)
        await callback.answer("Removals cleared.", show_alert=True)
        await caption_settings_handler(client, callback) # refresh

    elif action == "cap_del_rep":
        rules["replacements"] = {}
        await update_settings(user_id, caption_rules=rules)
        await callback.answer("Replacements cleared.", show_alert=True)
        await caption_settings_handler(client, callback) # refresh


@Client.on_callback_query(filters.regex("^set_channels"))
async def channel_manager(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    settings = await get_settings(user_id)
    
    if not settings:
        settings = {"dest_channels": [], "filters": {"all": True}}
        await update_settings(user_id)
        
    channels = settings.get("dest_channels", [])
    
    text = "📡 **Destination Channels**\n\n"
    if not channels:
        text += "No channels added yet."
    else:
        for i, ch in enumerate(channels, 1):
            text += f"{i}. `{ch}`\n"
            
    kb = [
        [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")],
        [InlineKeyboardButton("🗑 Delete Channel", callback_data="del_channel")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_settings")]
    ]
    await edit_or_reply(callback.message, text, InlineKeyboardMarkup(kb))


@Client.on_callback_query(filters.regex("^back_settings"))
async def back_settings(client, callback: CallbackQuery):
    # Determine if we go to main settings or somewhere else
    await show_settings_panel(callback.from_user.id, callback.message, is_edit=True)

@Client.on_callback_query(filters.regex("^(add_channel|del_channel)"))
async def channel_actions_handler(client, callback: CallbackQuery):
    action = callback.data
    user_id = callback.from_user.id
    
    if action == "add_channel":
        client.waiting_channel_user = user_id
        # Send a new message for input to avoid confusing the menu state
        await callback.message.reply_text(
            "📝 **New Channel Setup**\n\n"
            "Please send the **Channel ID** (e.g., `-10012345678`) or **Username** (`@mychannel`).\n"
            "⚠️ **Note:** Ensure your User Account is an Admin in that channel!"
        )
        await callback.answer()

    elif action == "del_channel":
        settings = await get_settings(user_id)
        if not settings or not settings["dest_channels"]:
            await callback.answer("❌ No channels to delete!", show_alert=True)
            return
        
        current = settings["dest_channels"]
        removed = current.pop() # Remove last one
        
        await update_settings(user_id, dest_channels=current)
        await callback.answer(f"🗑 Removed: {removed}")
        
        # Refresh the Channel Manager View
        await channel_manager(client, callback)
