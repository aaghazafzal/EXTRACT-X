import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import (get_session, get_settings, is_protected_channel, 
                      save_live_monitor, delete_live_monitor, get_live_monitors,
                      toggle_live_monitor, get_all_live_monitors, get_subscription)
from plugins.subscription import check_force_sub, check_user_access, PLANS
from config import API_ID, API_HASH, OWNER_ID

logger = logging.getLogger(__name__)

# Active live monitor tasks
live_tasks = {}  # user_id -> {source_channel -> Task}

# User states for setup
livebatch_states = {}  #user_id -> {"step": "...", "data": {...}}

@Client.on_message(filters.command("livebatch") & filters.private)
async def livebatch_command(client, message):
    if not await check_force_sub(client, message):
        return
    
    user_id = message.from_user.id
    
    # Check login
    if not await get_session(user_id):
        await message.reply_text("⛔ **Not Logged In**\n\nUse /login first to connect your account.")
        return
    
    # Check subscription
    allowed, reason, file_limit, remaining = await check_user_access(user_id)
    if not allowed:
        await message.reply_text(reason)
        return
    
    # Get plan details
    from database import get_subscription
    sub = await get_subscription(user_id)
    plan_type = sub.get("plan_type", "free") if sub else "free"
    
    # Owner bypass
    if user_id == int(OWNER_ID):
        limit = float('inf')
    else:
        limit = PLANS.get(plan_type, PLANS["free"])["live_monitor_limit"]
    
    if limit == 0:
        await message.reply_text(
            "🚫 **Premium Feature**\n\n"
            "Live Batch is only available for premium users.\n\n"
            "💎 **Upgrade your plan:**\n"
            "• Daily Pass: 2 live monitors\n"
            "• Monthly Pro: 5 live monitors\n"
            "• Ultra Pass: 15 live monitors\n\n"
            "Use /showplan to upgrade!"
        )
        return
    
    # Show menu
    await show_livebatch_menu(client, message, user_id, limit)

async def show_livebatch_menu(client, message, user_id, limit):
    monitors = await get_live_monitors(user_id)
    active_count = sum(1 for m in monitors if m["active"])
    
    text = (
        "📡 **LIVE BATCH MANAGER** 📡\n\n"
        "Automatically forward new messages from source channels in real-time!\n\n"
        f"🎯 **Your Limit:** {limit if limit != float('inf') else '∞'} monitors\n"
        f"⚡ **Active Now:** {active_count}/{limit if limit != float('inf') else '∞'}\n"
        f"📊 **Total Configured:** {len(monitors)}\n\n"
    )
    
    if monitors:
        text += "**Your Live Monitors:**\n"
        for idx, m in enumerate(monitors, 1):
            status = "🟢 Active" if m["active"] else "🔴 Paused"
            text += f"{idx}. `{m['source']}` → `{m['dest']}` {status}\n"
        text += "\n"
    
    text += (
        "**📌 How it works:**\n"
        "1. Add source channel + destination channel\n"
        "2. Bot monitors source for new messages\n"
        "3. Auto-forwards based on your filters\n"
        "4. Each source has its own destination\n\n"
        "Use buttons below to manage!"
    )
    
    buttons = []
    if active_count < limit or user_id == int(OWNER_ID):
        buttons.append([InlineKeyboardButton("➕ Add New Monitor", callback_data="live_add")])
    
    if monitors:
        buttons.append([InlineKeyboardButton("🗑 Remove Monitor", callback_data="live_remove")])
        buttons.append([InlineKeyboardButton("⏸ Pause/Resume", callback_data="live_toggle")])
        buttons.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="live_refresh")])
    
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="live_close")])
    
    if hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex("^live_"))
async def livebatch_callback_handler(client, callback):
    action = callback.data
    user_id = callback.from_user.id
    
    # Get limit
    sub = await get_subscription(user_id)
    plan_type = sub.get("plan_type", "free") if sub else "free"
    limit = PLANS.get(plan_type, PLANS["free"])["live_monitor_limit"] if user_id != int(OWNER_ID) else float('inf')
    
    if action == "live_add":
        monitors = await get_live_monitors(user_id)
        if len(monitors) >= limit and user_id != int(OWNER_ID):
            await callback.answer(f"⚠️ Limit reached! Max {limit} monitors.", show_alert=True)
            return
        
        livebatch_states[user_id] = {"step": "SOURCE"}
        await callback.message.edit_text(
            "➕ **Add New Live Monitor**\n\n"
            "**Step 1:** Send the **source channel link** or ID\n"
            "Example: `https://t.me/c/123456789/1` or `-100123456789`\n\n"
            "This is the channel you want to monitor for new messages."
        )
        await callback.answer()
    
    elif action == "live_remove":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors to remove!", show_alert=True)
            return
        
        text = "🗑 **Remove Monitor**\n\nSelect which monitor to remove:\n\n"
        buttons = []
        for idx, m in enumerate(monitors):
            buttons.append([
                InlineKeyboardButton(
                    f"{idx+1}. {m['source']} → {m['dest']}", 
                    callback_data=f"live_del_{m['source']}"
                )
            ])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="live_refresh")])
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        await callback.answer()
    
    elif action.startswith("live_del_"):
        source =action.replace("live_del_", "")
        try:
            source = int(source)
        except:
            pass
        await delete_live_monitor(user_id, source)
        
        # Stop task if running
        if user_id in live_tasks and source in live_tasks[user_id]:
            live_tasks[user_id][source].cancel()
            del live_tasks[user_id][source]
        
        await callback.answer("✅ Monitor removed!", show_alert=True)
        await show_livebatch_menu(client, callback.message, user_id, limit)
    
    elif action == "live_toggle":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        
        text = "⏸ **Pause/Resume Monitor**\n\nSelect monitor:\n\n"
        buttons = []
        for idx, m in enumerate(monitors):
            status_icon = "🟢" if m["active"] else "🔴"
            buttons.append([
                InlineKeyboardButton(
                    f"{status_icon} {idx+1}. {m['source']}", 
                    callback_data=f"live_tog_{m['source']}"
                )
            ])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="live_refresh")])
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        await callback.answer()
    
    elif action.startswith("live_tog_"):
        source = action.replace("live_tog_", "")
        try:
            source = int(source)
        except:
            pass
        
        monitors = await get_live_monitors(user_id)
        current = next((m for m in monitors if str(m["source"]) == str(source)), None)
        
        if current:
            new_state = not current["active"]
            await toggle_live_monitor(user_id, source, new_state)
            
            if new_state:
                # Start monitoring
                await start_monitor_task(client, user_id, source, current["dest"])
                await callback.answer("✅ Monitor activated!", show_alert=True)
            else:
                # Stop monitoring
                if user_id in live_tasks and source in live_tasks[user_id]:
                    live_tasks[user_id][source].cancel()
                    del live_tasks[user_id][source]
                await callback.answer("⏸ Monitor paused!", show_alert=True)
        
        await show_livebatch_menu(client, callback.message, user_id, limit)
    
    elif action == "live_refresh":
        await show_livebatch_menu(client, callback.message, user_id, limit)
        await callback.answer("♻️ Refreshed!")
    
    elif action == "live_close":
        await callback.message.delete()
        await callback.answer()

# Handle user input for live batch setup
async def handle_livebatch_input(client, message):
    user_id = message.from_user.id
    if user_id not in livebatch_states:
        return False
    
    state = livebatch_states[user_id]
    step = state["step"]
    text = message.text.strip()
    
    if step == "SOURCE":
        # Parse source channel
        try:
            if "t.me/c/" in text:
                parts = text.split("/")
                channel_id_str = parts[-2]
                source_id = int(f"-100{channel_id_str}")
            else:
                source_id = int(text)
        except:
            await message.reply_text("❌ Invalid channel ID/link. Try again.")
            return True
        
        # Check if protected
        if await is_protected_channel(source_id):
            await message.reply_text(
                "🔮 **Protected Channel Detected!** 🪄\n\n"
                "Whoa there! This channel is under a magical protection spell! ✨\n\n"
                "**मेरा जादू मुझ पर ही चलेगा!**\n"
                "_(My magic works for me only!)_\n\n"
                "You cannot extract from this protected channel. 🛡️"
            )
            del livebatch_states[user_id]
            return True
        
        state["source"] = source_id
        state["step"] = "DEST"
        await message.reply_text(
            f"✅ Source set: `{source_id}`\n\n"
            "**Step 2:** Send the **destination channel** ID\n"
            "Example: `-100123456789` or `@mychannel`\n\n"
            "New messages from source will be forwarded here."
        )
        return True
    
    elif step == "DEST":
        # Parse destination
        try:
            if text.startswith("@"):
                dest_id = text
            else:
                dest_id = int(text)
        except:
            await message.reply_text("❌ Invalid destination. Try again.")
            return True
        
        source_id = state["source"]
        
        # Save monitor
        await save_live_monitor(user_id, source_id, dest_id)
        
        # Start monitoring
        await start_monitor_task(client, user_id, source_id, dest_id)
        
        del livebatch_states[user_id]
        
        await message.reply_text(
            "✅ **Live Monitor Activated!** 🎉\n\n"
            f"📡 **Source:** `{source_id}`\n"
            f"📤 **Destination:** `{dest_id}`\n\n"
            "The bot is now monitoring for new messages!\n\n"
            "Use /livebatch to manage all monitors."
        )
        return True
    
    return False

# Start a monitoring task
async def start_monitor_task(client, user_id, source_channel, dest_channel):
    """Start a background task to monitor a channel"""
    
    if user_id not in live_tasks:
        live_tasks[user_id] = {}
    
    # Cancel existing task if any
    if source_channel in live_tasks[user_id]:
        live_tasks[user_id][source_channel].cancel()
    
    # Create new task
    task = asyncio.create_task(
        monitor_channel(client, user_id, source_channel, dest_channel)
    )
    live_tasks[user_id][source_channel] = task
    logger.info(f"Started live monitor: User {user_id}, Source {source_channel}")

async def monitor_channel(client, user_id, source_channel, dest_channel):
    """Background task that monitors a channel for new messages"""
    try:
        session = await get_session(user_id)
        settings = await get_settings(user_id)
        
        if not session:
            logger.error(f"No session for user {user_id}")
            return
        
        # Create userbot client
        userbot = Client(
            f"live_{user_id}_{source_channel}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session,
            in_memory=True
        )
        
        await userbot.start()
        
        # Register handler for new messages in this channel
        @userbot.on_message(filters.chat(source_channel))
        async def live_forward_handler(client, msg):
            try:
                # Apply filters
                filters_set = settings.get("filters", {"all": True})
                caption_rules = settings.get("caption_rules", {})
                
                # Type check
                ok = False
                if filters_set.get("all"):
                    ok = True
                elif msg.media:
                    from pyrogram import enums
                    mtype = msg.media
                    if mtype == enums.MessageMediaType.PHOTO and filters_set.get("photo"): ok = True
                    elif mtype == enums.MessageMediaType.VIDEO and filters_set.get("video"): ok = True
                    elif mtype == enums.MessageMediaType.DOCUMENT and filters_set.get("document"): ok = True
                    elif filters_set.get("media"): ok = True
                elif msg.text and filters_set.get("text"):
                    ok = True
                
                if not ok:
                    return
                
                # Caption processing
                final_caption = msg.caption or (msg.text if not msg.media else "") or ""
                if final_caption:
                    for rem in caption_rules.get("removals", []): 
                        final_caption = final_caption.replace(rem, "")
                    for old, new in caption_rules.get("replacements", {}).items(): 
                        final_caption = final_caption.replace(old, new)
                    final_caption = final_caption.strip()
                    p = caption_rules.get("prefix", "")
                    s = caption_rules.get("suffix", "")
                    if p: final_caption = f"{p}\n{final_caption}"
                    if s: final_caption = f"{final_caption}\n{s}"
                else:
                    p = caption_rules.get("prefix", "")
                    s = caption_rules.get("suffix", "")
                    parts = [x for x in [p, s] if x]
                    if parts: final_caption = "\n".join(parts)
                
                # Forward to destination
                try:
                    d_id = int(dest_channel) if isinstance(dest_channel, str) and dest_channel.lstrip('-').isdigit() else dest_channel
                except:
                    d_id = dest_channel
                
                await userbot.copy_message(
                    chat_id=d_id,
                    from_chat_id=source_channel,
                    message_id=msg.id,
                    caption=final_caption
                )
                
                logger.info(f"Live forwarded: {source_channel} → {dest_channel} (User {user_id})")
                
            except Exception as e:
                logger.error(f"Live forward error: {e}")
        
        # Keep alive
        await asyncio.Event().wait()  # Wait forever
        
    except asyncio.CancelledError:
        logger.info(f"Monitor cancelled: User {user_id}, Source {source_channel}")
        if 'userbot' in locals():
            await userbot.stop()
    except Exception as e:
        logger.error(f"Monitor error for user {user_id}: {e}")
        if 'userbot' in locals():
            await userbot.stop()

# Initialize all active monitors on bot startup
async def init_live_monitors(bot_client):
    """Start all active monitors when bot starts"""
    try:
        all_monitors = await get_all_live_monitors()
        logger.info(f"Initializing {len(all_monitors)} active live monitors...")
        
        for monitor in all_monitors:
            user_id = monitor["user_id"]
            source = monitor["source"]
            dest = monitor["dest"]
            
            await start_monitor_task(bot_client, user_id, source, dest)
        
        logger.info("Live monitors initialized successfully!")
    except Exception as e:
        logger.error(f"Failed to initialize live monitors: {e}")
