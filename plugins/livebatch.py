import asyncio
import logging
import os
import time
import datetime

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, ChannelInvalid, ChatIdInvalid

from database import (
    get_session, get_settings, is_protected_channel,
    save_live_monitor, delete_live_monitor, get_live_monitors,
    toggle_live_monitor, get_all_live_monitors,
    increment_live_stats, update_live_monitor_meta
)
from plugins.subscription import check_force_sub, get_resolved_plan, PLANS
from config import API_ID, API_HASH, OWNER_ID

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# STATE STORES
# ══════════════════════════════════════════════════
live_tasks = {}          # user_id → {source_channel → Task}
livebatch_states = {}    # user_id → {step, data}

# ══════════════════════════════════════════════════
# HELPER: parse channel link/id
# ══════════════════════════════════════════════════
def parse_channel_input(text: str):
    """
    Accepts:
    - https://t.me/c/12345678/1  → -10012345678
    - https://t.me/username      → '@username'
    - -100123456789              → -100123456789
    - @username                  → '@username'
    Returns (channel_id, is_private) or raises ValueError
    """
    text = text.strip().rstrip("/").split("?")[0]
    if "t.me/c/" in text:
        parts = text.split("/")
        idx = parts.index("c")
        ch_id = int(f"-100{parts[idx+1]}")
        return ch_id, True
    elif "t.me/" in text:
        parts = text.split("t.me/")
        username = parts[-1].split("/")[0]
        return f"@{username}", False
    elif text.startswith("-100") or (text.lstrip("-").isdigit()):
        return int(text), True
    elif text.startswith("@"):
        return text, False
    else:
        raise ValueError("Invalid channel input")

def fmt_ts(ts):
    if not ts: return "Never"
    return datetime.datetime.fromtimestamp(ts).strftime("%d %b • %I:%M %p")

# ══════════════════════════════════════════════════
# LIVE BATCH MENU
# ══════════════════════════════════════════════════
async def get_monitor_limit(user_id):
    if user_id == int(OWNER_ID):
        return float('inf')
    _, plan, _, _ = await get_resolved_plan(user_id)
    return plan["live_monitor_limit"]

async def show_livebatch_menu(target, user_id, limit, is_edit=False):
    monitors = await get_live_monitors(user_id)
    active_count = sum(1 for m in monitors if m["active"])
    limit_str = str(int(limit)) if limit != float('inf') else "∞"
    
    now = time.time()
    
    # Header
    text = (
        "╔══════════════════════╗\n"
        "║   📡  LIVE MONITOR HUB   ║\n"
        "╚══════════════════════╝\n\n"
        f"🎯 **Limit:** `{active_count}/{limit_str}` active\n"
        f"📊 **Configured:** `{len(monitors)}`\n"
        f"⏰ **Last Refresh:** `{fmt_ts(now)}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    if monitors:
        text += "**📌 Your Monitors:**\n\n"
        for idx, m in enumerate(monitors, 1):
            status_icon = "🟢" if m["active"] else "⏸"
            title = m.get("source_title") or str(m["source"])
            count = m.get("msg_count", 0)
            last = fmt_ts(m.get("last_seen")) if m.get("last_seen") else "No messages yet"
            silent_tag = " 🔇" if m.get("silent") else ""
            text += (
                f"**{idx}.** {status_icon} **{title}**{silent_tag}\n"
                f"   • Source: `{m['source']}`\n"
                f"   • Dest: `{m['dest']}`\n"
                f"   • Forwarded: `{count}` msgs\n"
                f"   • Last: `{last}`\n\n"
            )
    else:
        text += (
            "📭 **No monitors yet.**\n\n"
            "**How it works:**\n"
            "1️⃣ Add a source channel link\n"
            "2️⃣ Set destination channel\n"
            "3️⃣ Bot auto-forwards every new post instantly\n"
            "4️⃣ Restricted channels? Bot downloads & re-uploads!\n"
        )
    
    # Buttons
    buttons = []
    if active_count < limit or user_id == int(OWNER_ID):
        buttons.append([InlineKeyboardButton("➕ Add New Monitor", callback_data="live_add")])
    else:
        buttons.append([InlineKeyboardButton("⛔ Limit Reached — Upgrade Plan", callback_data="live_upgrade")])
    
    if monitors:
        buttons.append([
            InlineKeyboardButton("⏸ Pause/Resume", callback_data="live_toggle"),
            InlineKeyboardButton("🗑 Remove", callback_data="live_remove"),
        ])
        buttons.append([
            InlineKeyboardButton("📊 Stats", callback_data="live_stats"),
            InlineKeyboardButton("🔇 Silent Mode", callback_data="live_silent_menu"),
        ])
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="live_refresh")])
    
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="live_close")])
    kb = InlineKeyboardMarkup(buttons)
    
    try:
        if is_edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.reply_text(text, reply_markup=kb)
    except Exception:
        pass

# ══════════════════════════════════════════════════
# /livebatch COMMAND
# ══════════════════════════════════════════════════
@Client.on_message(filters.command("livebatch") & filters.private)
async def livebatch_command(client, message):
    if not await check_force_sub(client, message):
        return
    
    user_id = message.from_user.id
    
    # Must be logged in
    if not await get_session(user_id):
        await message.reply_text(
            "⛔ **Login Required**\n\n"
            "Live Monitor uses your Telegram account to watch channels.\n"
            "Use /login to connect your account first."
        )
        return
    
    limit = await get_monitor_limit(user_id)
    
    if limit == 0:
        await message.reply_text(
            "🚫 **Premium Feature**\n\n"
            "Live Monitor is available for **Premium users only.**\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📡 **Plan Limits:**\n"
            "⚡ Daily Pass: `2` monitors\n"
            "💎 Monthly Pro: `5` monitors\n"
            "🚀 Ultra Pass: `15` monitors\n"
            "♾️ Lifetime: `30` monitors\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Use /showplan to upgrade! 🚀",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 View Plans", callback_data="show_plans_back")]
            ])
        )
        return
    
    await show_livebatch_menu(message, user_id, limit, is_edit=False)

# ══════════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════════
@Client.on_callback_query(filters.regex("^live_"))
async def livebatch_callback_handler(client, callback):
    action = callback.data
    user_id = callback.from_user.id
    limit = await get_monitor_limit(user_id)
    
    # ── ADD ──
    if action == "live_add":
        monitors = await get_live_monitors(user_id)
        active = sum(1 for m in monitors if m["active"])
        if active >= limit and user_id != int(OWNER_ID):
            await callback.answer(f"⚠️ Limit reached! Max {int(limit)} monitors on your plan.", show_alert=True)
            return
        livebatch_states[user_id] = {"step": "SOURCE"}
        await callback.message.edit_text(
            "➕ **Add Live Monitor — Step 1/2**\n\n"
            "📡 **Send the Source Channel:**\n\n"
            "Accepted formats:\n"
            "• `https://t.me/c/123456789/1` _(private)_\n"
            "• `https://t.me/channelname` _(public)_\n"
            "• `-100123456789` _(channel ID)_\n"
            "• `@channelname` _(username)_\n\n"
            "⚠️ You must have joined / be a member of this channel.\n\n"
            "_Send the link or ID now:_",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="live_cancel_setup")]
            ])
        )
        await callback.answer()
    
    # ── CANCEL SETUP ──
    elif action == "live_cancel_setup":
        livebatch_states.pop(user_id, None)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
        await callback.answer("Cancelled.")
    
    # ── REMOVE ──
    elif action == "live_remove":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors to remove!", show_alert=True)
            return
        buttons = []
        for m in monitors:
            title = m.get("source_title") or str(m["source"])
            buttons.append([InlineKeyboardButton(
                f"🗑 {title}", callback_data=f"live_del_{m['source']}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "🗑 **Remove Monitor**\n\nTap a monitor to remove it permanently:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback.answer()
    
    elif action.startswith("live_del_"):
        source_str = action[len("live_del_"):]
        try: source = int(source_str)
        except: source = source_str
        await delete_live_monitor(user_id, source)
        # Cancel task
        if user_id in live_tasks and source in live_tasks[user_id]:
            live_tasks[user_id][source].cancel()
            del live_tasks[user_id][source]
        await callback.answer("✅ Monitor removed!", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
    
    # ── TOGGLE ──
    elif action == "live_toggle":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        buttons = []
        for m in monitors:
            status_icon = "🟢" if m["active"] else "⏸"
            title = m.get("source_title") or str(m["source"])
            buttons.append([InlineKeyboardButton(
                f"{status_icon} {title}", callback_data=f"live_tog_{m['source']}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "⏸ **Pause / Resume**\n\nTap a monitor to toggle:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback.answer()
    
    elif action.startswith("live_tog_"):
        source_str = action[len("live_tog_"):]
        try: source = int(source_str)
        except: source = source_str
        monitors = await get_live_monitors(user_id)
        m = next((x for x in monitors if str(x["source"]) == str(source)), None)
        if m:
            new_state = not m["active"]
            await toggle_live_monitor(user_id, source, new_state)
            if new_state:
                await start_monitor_task(client, user_id, source, m["dest"])
                await callback.answer("▶️ Monitor resumed!", show_alert=True)
            else:
                if user_id in live_tasks and source in live_tasks[user_id]:
                    live_tasks[user_id][source].cancel()
                    del live_tasks[user_id][source]
                await callback.answer("⏸ Monitor paused!", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
    
    # ── STATS ──
    elif action == "live_stats":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        text = "📊 **Live Monitor Stats**\n\n"
        for idx, m in enumerate(monitors, 1):
            title = m.get("source_title") or str(m["source"])
            status = "🟢 Active" if m["active"] else "⏸ Paused"
            task_alive = user_id in live_tasks and m["source"] in live_tasks[user_id]
            engine = "✅ Running" if task_alive else "⚠️ Not Running"
            text += (
                f"**{idx}. {title}**\n"
                f"   • Status: {status}\n"
                f"   • Engine: {engine}\n"
                f"   • Forwarded: `{m.get('msg_count', 0)}` messages\n"
                f"   • Last Active: `{fmt_ts(m.get('last_seen'))}`\n\n"
            )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    
    # ── SILENT MODE MENU ──
    elif action == "live_silent_menu":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        buttons = []
        for m in monitors:
            title = m.get("source_title") or str(m["source"])
            mode = "🔇 Silent ON" if m.get("silent") else "🔔 Silent OFF"
            buttons.append([InlineKeyboardButton(
                f"{mode} — {title}", callback_data=f"live_siltog_{m['source']}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "🔇 **Silent Mode** — messages are forwarded without notification\n\nToggle per monitor:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback.answer()
    
    elif action.startswith("live_siltog_"):
        source_str = action[len("live_siltog_"):]
        try: source = int(source_str)
        except: source = source_str
        monitors = await get_live_monitors(user_id)
        m = next((x for x in monitors if str(x["source"]) == str(source)), None)
        if m:
            new_silent = not m.get("silent", False)
            await update_live_monitor_meta(user_id, source, silent=new_silent)
            mode = "🔇 Silent ON" if new_silent else "🔔 Silent OFF"
            await callback.answer(f"Set to {mode}!", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
    
    # ── UPGRADE ──
    elif action == "live_upgrade":
        await callback.answer("Upgrade your plan to add more monitors!", show_alert=True)
    
    # ── REFRESH ──
    elif action == "live_refresh":
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
        await callback.answer("🔄 Refreshed!")
    
    # ── CLOSE ──
    elif action == "live_close":
        try:
            await callback.message.delete()
        except: pass
        await callback.answer()

# ══════════════════════════════════════════════════
# TEXT INPUT HANDLER (setup steps)
# ══════════════════════════════════════════════════
async def handle_livebatch_input(client, message):
    user_id = message.from_user.id
    if user_id not in livebatch_states:
        return False
    
    state = livebatch_states[user_id]
    step = state["step"]
    text = message.text.strip() if message.text else ""
    
    if step == "SOURCE":
        try:
            source_id, is_private = parse_channel_input(text)
        except ValueError:
            await message.reply_text(
                "❌ **Invalid Format**\n\n"
                "Send channel link or ID:\n"
                "• `https://t.me/c/123456789/1`\n"
                "• `https://t.me/channelname`\n"
                "• `-100123456789`"
            )
            return True
        
        # Protected check
        if isinstance(source_id, int) and await is_protected_channel(source_id):
            del livebatch_states[user_id]
            await message.reply_text(
                "🔮 **Protected Channel!**\n\n"
                "This channel is protected from extraction.\n"
                "Contact /support if you think this is a mistake."
            )
            return True
        
        # Try to get title
        source_title = str(source_id)
        try:
            session = await get_session(user_id)
            if session:
                ub = Client(f"tmp_{user_id}", api_id=API_ID, api_hash=API_HASH, session_string=session, in_memory=True)
                await ub.start()
                try:
                    chat = await ub.get_chat(source_id)
                    source_title = chat.title or chat.first_name or str(source_id)
                finally:
                    await ub.stop()
        except Exception:
            pass
        
        state["source"] = source_id
        state["source_title"] = source_title
        state["step"] = "DEST"
        
        await message.reply_text(
            f"✅ **Source Set!**\n\n"
            f"📡 **Channel:** `{source_title}`\n"
            f"🆔 **ID:** `{source_id}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Step 2/2 — Set Destination:**\n\n"
            "Send your destination channel ID or username:\n"
            "• `-100123456789`\n"
            "• `@mychannel`\n\n"
            "⚠️ Bot must be **Admin** in the destination channel!"
        )
        return True
    
    elif step == "DEST":
        try:
            dest_id, _ = parse_channel_input(text)
        except ValueError:
            await message.reply_text("❌ Invalid destination. Send channel ID or @username.")
            return True
        
        source_id = state["source"]
        source_title = state.get("source_title", str(source_id))
        
        # Save to DB
        await save_live_monitor(user_id, source_id, dest_id)
        await update_live_monitor_meta(user_id, source_id, source_title=source_title)
        
        # Start monitor task
        await start_monitor_task(client, user_id, source_id, dest_id)
        
        del livebatch_states[user_id]
        
        await message.reply_text(
            "╔══════════════════════╗\n"
            "║  📡  MONITOR ACTIVE!  ║\n"
            "╚══════════════════════╝\n\n"
            f"✅ **Live Monitor is running!**\n\n"
            f"📡 **Source:** `{source_title}`\n"
            f"📤 **Destination:** `{dest_id}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ **What happens next:**\n"
            "• Every new message in source will be auto-forwarded\n"
            "• Restricted content? Bot downloads & re-uploads it\n"
            "• Your filters & captions from /settings apply\n"
            "• Silent mode available via /livebatch\n\n"
            "Use /livebatch to manage all your monitors! 🎛️",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Manage Monitors", callback_data="live_refresh")]
            ])
        )
        return True
    
    return False

# ══════════════════════════════════════════════════
# MONITOR TASK ENGINE
# ══════════════════════════════════════════════════
async def start_monitor_task(client, user_id, source_channel, dest_channel):
    if user_id not in live_tasks:
        live_tasks[user_id] = {}
    if source_channel in live_tasks[user_id]:
        live_tasks[user_id][source_channel].cancel()
    task = asyncio.create_task(
        monitor_channel(client, user_id, source_channel, dest_channel)
    )
    live_tasks[user_id][source_channel] = task
    logger.info(f"Started live monitor: User {user_id}, Source {source_channel}")

async def monitor_channel(client, user_id, source_channel, dest_channel):
    """Background task: monitors source channel and forwards new content."""
    userbot = None
    try:
        session = await get_session(user_id)
        if not session:
            logger.error(f"No session for user {user_id} — cannot start monitor.")
            return
        
        userbot = Client(
            f"live_{user_id}_{source_channel}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session,
            in_memory=True
        )
        await userbot.start()
        logger.info(f"Userbot started for live monitor: {user_id}/{source_channel}")
        
        # Determine dest_id
        try:
            d_id = int(dest_channel) if str(dest_channel).lstrip('-').isdigit() else dest_channel
        except:
            d_id = dest_channel
        
        @userbot.on_message(filters.chat(source_channel) & ~filters.service)
        async def live_handler(ub_client, msg):
            try:
                # Reload settings dynamically (so user changes apply without restarting monitor)
                settings = await get_settings(user_id) or {}
                filters_cfg = settings.get("filters", {"all": True})
                caption_rules = settings.get("caption_rules", {})
                custom_thumb_id = settings.get("custom_thumbnail")
                
                # Get silent mode from DB
                monitors = await get_live_monitors(user_id)
                monitor_cfg = next(
                    (m for m in monitors if str(m["source"]) == str(source_channel)), {}
                )
                silent = monitor_cfg.get("silent", False)
                
                # ── Filter check ──
                ok = False
                if filters_cfg.get("all"):
                    ok = True
                elif msg.media:
                    mt = msg.media
                    if mt == enums.MessageMediaType.PHOTO and filters_cfg.get("photo"): ok = True
                    elif mt == enums.MessageMediaType.VIDEO and filters_cfg.get("video"): ok = True
                    elif mt == enums.MessageMediaType.DOCUMENT and filters_cfg.get("document"): ok = True
                    elif mt == enums.MessageMediaType.AUDIO and filters_cfg.get("audio"): ok = True
                    elif mt == enums.MessageMediaType.ANIMATION and filters_cfg.get("video"): ok = True
                    elif filters_cfg.get("media"):
                        if mt in [
                            enums.MessageMediaType.PHOTO, enums.MessageMediaType.VIDEO,
                            enums.MessageMediaType.DOCUMENT, enums.MessageMediaType.AUDIO,
                            enums.MessageMediaType.VOICE, enums.MessageMediaType.ANIMATION
                        ]:
                            ok = True
                elif msg.text and filters_cfg.get("text"):
                    ok = True
                
                if not ok:
                    return
                
                # ── Caption processing ──
                raw_cap = msg.caption or (msg.text if not msg.media else "") or ""
                cap = raw_cap
                if cap:
                    for rem in caption_rules.get("removals", []):
                        cap = cap.replace(rem, "")
                    for old, new in caption_rules.get("replacements", {}).items():
                        cap = cap.replace(old, new)
                    cap = cap.strip()
                p = caption_rules.get("prefix", "")
                s = caption_rules.get("suffix", "")
                if p: cap = f"{p}\n{cap}" if cap else p
                if s: cap = f"{cap}\n{s}" if cap else s
                if caption_rules.get("remove_caption"):
                    cap = ""
                
                # ── Forward attempt: fast copy first ──
                forwarded = False
                try:
                    await ub_client.copy_message(
                        chat_id=d_id,
                        from_chat_id=source_channel,
                        message_id=msg.id,
                        caption=cap if cap else None,
                        disable_notification=silent
                    )
                    forwarded = True
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 2)
                    try:
                        await ub_client.copy_message(
                            chat_id=d_id,
                            from_chat_id=source_channel,
                            message_id=msg.id,
                            caption=cap if cap else None,
                            disable_notification=silent
                        )
                        forwarded = True
                    except: pass
                except Exception as e:
                    err = str(e)
                    if not any(x in err for x in ["FORWARDS_RESTRICTED", "restricted", "CHAT_FORWARD"]):
                        logger.warning(f"Live copy_message error: {e}")
                        return
                    # Fall through to DL+Upload
                
                # ── DL + Upload fallback for restricted channels ──
                if not forwarded:
                    f_path = None
                    thumb_path = None
                    try:
                        if msg.text:
                            await ub_client.send_message(d_id, cap or msg.text, disable_notification=silent)
                            forwarded = True
                        elif msg.media:
                            f_path = await ub_client.download_media(msg)
                            if not f_path:
                                return
                            
                            # Thumbnail
                            if custom_thumb_id:
                                try:
                                    thumb_path = await client.download_media(custom_thumb_id)
                                except: pass
                            elif msg.video and msg.video.thumbs:
                                try:
                                    thumb_path = await ub_client.download_media(msg.video.thumbs[0].file_id)
                                except: pass
                            
                            kw = {"disable_notification": silent}
                            if msg.photo:
                                await ub_client.send_photo(d_id, f_path, caption=cap or None, **kw)
                            elif msg.video:
                                await ub_client.send_video(
                                    d_id, f_path, caption=cap or None,
                                    duration=msg.video.duration,
                                    width=msg.video.width, height=msg.video.height,
                                    thumb=thumb_path, **kw
                                )
                            elif msg.document:
                                await ub_client.send_document(d_id, f_path, caption=cap or None, force_document=True, **kw)
                            elif msg.audio:
                                await ub_client.send_audio(
                                    d_id, f_path, caption=cap or None,
                                    duration=msg.audio.duration,
                                    performer=msg.audio.performer,
                                    title=msg.audio.title, **kw
                                )
                            elif msg.voice:
                                await ub_client.send_voice(d_id, f_path, caption=cap or None, duration=msg.voice.duration, **kw)
                            elif msg.animation:
                                await ub_client.send_animation(d_id, f_path, caption=cap or None, **kw)
                            elif msg.sticker:
                                await ub_client.send_sticker(d_id, f_path, **kw)
                            else:
                                await ub_client.send_document(d_id, f_path, caption=cap or None, **kw)
                            forwarded = True
                    finally:
                        for p_path in [f_path, thumb_path]:
                            if p_path and os.path.exists(p_path):
                                try: os.remove(p_path)
                                except: pass
                
                if forwarded:
                    await increment_live_stats(user_id, source_channel)
                    logger.info(f"Live forwarded: {source_channel} → {dest_channel} (User {user_id})")
            
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Live handler error [{user_id}/{source_channel}]: {e}")
        
        # Keep alive until cancelled
        await asyncio.Event().wait()
    
    except asyncio.CancelledError:
        logger.info(f"Monitor cancelled: User {user_id} / Source {source_channel}")
    except Exception as e:
        logger.error(f"Monitor crash [{user_id}/{source_channel}]: {e}")
    finally:
        if userbot:
            try:
                await userbot.stop()
            except: pass

# ══════════════════════════════════════════════════
# STARTUP: restore all active monitors
# ══════════════════════════════════════════════════
async def init_live_monitors(bot_client):
    """Start all active live monitors on bot startup."""
    try:
        all_monitors = await get_all_live_monitors()
        logger.info(f"Initializing {len(all_monitors)} active live monitors...")
        for m in all_monitors:
            await start_monitor_task(bot_client, m["user_id"], m["source"], m["dest"])
        logger.info("Live monitors initialized successfully!")
    except Exception as e:
        logger.error(f"Failed to initialize live monitors: {e}")
