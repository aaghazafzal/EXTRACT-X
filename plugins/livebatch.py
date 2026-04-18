import asyncio
import logging
import os
import time
import datetime

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from database import (
    get_session, get_settings, is_protected_channel,
    save_live_monitor, delete_live_monitor, get_live_monitors,
    toggle_live_monitor, get_all_live_monitors,
    increment_live_stats, update_live_monitor_meta, increment_channel_stat
)
from plugins.subscription import check_force_sub, get_resolved_plan, PLANS
from config import API_ID, API_HASH, OWNER_ID

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════
MAX_DL_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB limit for DL+Upload

# ══════════════════════════════════════════════════
# STATE STORES
# ══════════════════════════════════════════════════
live_tasks   = {}      # (user_id, source) → asyncio.Task (queue processor)
live_queues  = {}      # (user_id, source) → asyncio.Queue
live_progress = {}     # (user_id, source) → progress_dict
livebatch_states = {}  # user_id → setup step dict

# ══════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════
def parse_channel_input(text: str):
    text = text.strip().rstrip("/").split("?")[0]
    if "t.me/c/" in text:
        parts = text.split("/")
        idx = parts.index("c")
        return int(f"-100{parts[idx+1]}"), True
    elif "t.me/" in text:
        username = text.split("t.me/")[-1].split("/")[0]
        return f"@{username}", False
    elif text.lstrip("-").isdigit():
        return int(text), True
    elif text.startswith("@"):
        return text, False
    raise ValueError("Invalid channel input")

def fmt_ts(ts):
    if not ts: return "Never"
    return datetime.datetime.fromtimestamp(ts).strftime("%d %b • %I:%M %p")

def fmt_size(b):
    if b is None: return "?"
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def get_file_size(msg):
    if msg.video:    return msg.video.file_size or 0
    if msg.document: return msg.document.file_size or 0
    if msg.audio:    return msg.audio.file_size or 0
    if msg.voice:    return msg.voice.file_size or 0
    if msg.animation:return msg.animation.file_size or 0
    return 0

def make_progress_bar(done, total, length=8):
    if total == 0: return "░" * length
    ratio = min(done / total, 1.0)
    filled = int(length * ratio)
    return "▓" * filled + "░" * (length - filled)

async def get_monitor_limit(user_id):
    if user_id == int(OWNER_ID): return float('inf')
    _, plan, _, _ = await get_resolved_plan(user_id)
    return plan["live_monitor_limit"]

def progress_key(user_id, source): return (user_id, str(source))

def init_progress(user_id, source):
    key = progress_key(user_id, source)
    live_progress[key] = {
        "forwarded": 0,
        "pending":   0,
        "skipped":   0,
        "errors":    0,
        "method":    "idle",
        "current_file": "",
        "current_size": 0,
        "downloaded_size": 0,
        "start_time": time.time(),
        "last_update": time.time(),
    }
    return key

# ══════════════════════════════════════════════════
# MAIN MENU BUILDER
# ══════════════════════════════════════════════════
async def show_livebatch_menu(target, user_id, limit, is_edit=False):
    monitors = await get_live_monitors(user_id)
    active_count = sum(1 for m in monitors if m["active"])
    limit_str = str(int(limit)) if limit != float('inf') else "∞"

    text = (
        "╔══════════════════════╗\n"
        "║   📡  LIVE MONITOR HUB   ║\n"
        "╚══════════════════════╝\n\n"
        f"⚡ **Slots Used:** `{active_count}/{limit_str}`\n"
        f"📊 **Total Configured:** `{len(monitors)}`\n"
        f"🕐 **Refreshed:** `{fmt_ts(time.time())}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if monitors:
        text += "**📌 Your Live Monitors:**\n\n"
        for idx, m in enumerate(monitors, 1):
            icon = "🟢" if m["active"] else "⏸"
            title = m.get("source_title") or str(m["source"])
            key = progress_key(user_id, m["source"])
            prog = live_progress.get(key, {})
            fwd  = m.get("msg_count", 0)
            pending = prog.get("pending", 0)
            skip = prog.get("skipped", 0)
            silent_tag = " 🔇" if m.get("silent") else ""
            task_alive = False
            if user_id in live_tasks and m["source"] in live_tasks[user_id]:
                task = live_tasks[user_id][m["source"]]
                task_alive = not task.done()
            engine = "⚙️" if task_alive else "💤"
            text += (
                f"**{idx}.** {icon}{silent_tag} {engine} **{title}**\n"
                f"   ✅ `{fwd}` done  •  🕐 `{pending}` pending  •  ⏭ `{skip}` skipped\n"
                f"   📤 → `{m['dest']}`\n\n"
            )
    else:
        text += (
            "📭 **No monitors yet.**\n\n"
            "**How it works:**\n"
            "1️⃣ Add source channel link\n"
            "2️⃣ Set destination channel\n"
            "3️⃣ Bot auto-forwards every new post instantly\n"
            "4️⃣ Restricted? Downloads & re-uploads up to **2 GB**!\n"
            "5️⃣ Messages queued — nothing is ever missed!\n"
        )

    buttons = []
    if active_count < limit or user_id == int(OWNER_ID):
        buttons.append([InlineKeyboardButton("➕ Add New Monitor", callback_data="live_add")])
    else:
        buttons.append([InlineKeyboardButton("⛔ Limit Reached — Upgrade", callback_data="live_upgrade")])

    if monitors:
        # Per-monitor stats buttons
        for m in monitors:
            title = m.get("source_title") or str(m["source"])[:20]
            icon = "🟢" if m["active"] else "⏸"
            buttons.append([
                InlineKeyboardButton(f"📊 {icon} {title}", callback_data=f"live_mon_stat_{m['source']}"),
            ])

        buttons.append([
            InlineKeyboardButton("⏸ Pause/Resume", callback_data="live_toggle"),
            InlineKeyboardButton("🗑 Remove", callback_data="live_remove"),
        ])
        buttons.append([
            InlineKeyboardButton("🔇 Silent Mode", callback_data="live_silent_menu"),
            InlineKeyboardButton("🔄 Refresh", callback_data="live_refresh"),
        ])

    buttons.append([InlineKeyboardButton("❌ Close", callback_data="live_close")])
    kb = InlineKeyboardMarkup(buttons)

    try:
        if is_edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.reply_text(text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"show_livebatch_menu error: {e}")

# ══════════════════════════════════════════════════
# /livebatch COMMAND
# ══════════════════════════════════════════════════
@Client.on_message(filters.command("livebatch") & filters.private)
async def livebatch_command(client, message):
    if not await check_force_sub(client, message): return
    user_id = message.from_user.id

    if not await get_session(user_id):
        await message.reply_text(
            "⛔ **Login Required**\n\n"
            "Live Monitor needs your Telegram account to watch channels.\n"
            "Use /login first to connect."
        )
        return

    limit = await get_monitor_limit(user_id)
    if limit == 0:
        await message.reply_text(
            "🚫 **Premium Feature**\n\n"
            "Live Monitor is only for **Premium users.**\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📡 **Per-Plan Limits:**\n"
            "⚡ Daily Pass — `2` monitors\n"
            "💎 Monthly Pro — `5` monitors\n"
            "🚀 Ultra Pass — `15` monitors\n"
            "♾️ Lifetime — `30` monitors\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
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
            await callback.answer(f"⚠️ Limit reached! Max {int(limit)} on your plan.", show_alert=True)
            return
        livebatch_states[user_id] = {"step": "SOURCE"}
        await callback.message.edit_text(
            "➕ **Add Live Monitor — Step 1/2**\n\n"
            "📡 **Send the Source Channel:**\n\n"
            "Accepted formats:\n"
            "• `https://t.me/c/123456789/1` _(private)_\n"
            "• `https://t.me/channelname` _(public)_\n"
            "• `-100123456789` _(raw ID)_\n"
            "• `@channelname` _(username)_\n\n"
            "⚠️ You must be a **member** of this channel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="live_cancel_setup")]
            ])
        )
        await callback.answer()

    elif action == "live_cancel_setup":
        livebatch_states.pop(user_id, None)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
        await callback.answer("Cancelled.")

    # ── PER-MONITOR STATS ──
    elif action.startswith("live_mon_stat_"):
        source_str = action[len("live_mon_stat_"):]
        try: source = int(source_str)
        except: source = source_str

        monitors = await get_live_monitors(user_id)
        m = next((x for x in monitors if str(x["source"]) == str(source)), None)
        if not m:
            await callback.answer("Monitor not found!", show_alert=True)
            return

        key = progress_key(user_id, source)
        prog = live_progress.get(key, {})
        title = m.get("source_title") or str(source)
        status = "🟢 Active" if m["active"] else "⏸ Paused"
        task_alive = user_id in live_tasks and source in live_tasks.get(user_id, {})
        engine_status = "✅ Engine Running" if task_alive else "💤 Engine Stopped"
        fwd = m.get("msg_count", 0)
        pending = prog.get("pending", 0)
        skipped = prog.get("skipped", 0)
        errors = prog.get("errors", 0)
        method = prog.get("method", "idle")
        current_file = prog.get("current_file", "—")
        current_size = prog.get("current_size", 0)
        dl_done = prog.get("downloaded_size", 0)
        last_active = fmt_ts(m.get("last_seen"))
        silent_mode = "🔇 ON" if m.get("silent") else "🔔 OFF"
        start_t = prog.get("start_time", 0)
        runtime = ""
        if start_t:
            secs = int(time.time() - start_t)
            h, rem = divmod(secs, 3600)
            mins, s = divmod(rem, 60)
            runtime = f"{h}h {mins}m {s}s"

        # Build progress bar if DL in progress
        dl_bar = ""
        if method == "dl_upload" and current_size > 0:
            dl_bar = (
                f"\n\n**📥 DL+Upload Progress:**\n"
                f"`{make_progress_bar(dl_done, current_size)}` "
                f"`{fmt_size(dl_done)}/{fmt_size(current_size)}`\n"
                f"📄 `{current_file}`"
            )

        text = (
            f"╔══════════════════════╗\n"
            f"║  📊  MONITOR STATS  ║\n"
            f"╚══════════════════════╝\n\n"
            f"📡 **Channel:** `{title}`\n"
            f"🆔 **Source ID:** `{source}`\n"
            f"📤 **Destination:** `{m['dest']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔋 **Status:** {status}\n"
            f"⚙️ **Engine:** {engine_status}\n"
            f"🔇 **Silent Mode:** {silent_mode}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ **Forwarded:** `{fwd}` messages\n"
            f"🕐 **Queue Pending:** `{pending}` messages\n"
            f"⏭ **Skipped (>2GB):** `{skipped}` files\n"
            f"❌ **Errors:** `{errors}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ **Current Method:** `{method}`\n"
            f"🕒 **Last Message:** `{last_active}`\n"
            f"⏱ **Runtime:** `{runtime}`"
            f"{dl_bar}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Stats", callback_data=f"live_mon_stat_{source}")],
            [InlineKeyboardButton("⬅️ Back to Hub", callback_data="live_refresh")],
        ])
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except: pass
        await callback.answer()

    # ── REMOVE ──
    elif action == "live_remove":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors to remove!", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(
            f"🗑 {m.get('source_title') or str(m['source'])}",
            callback_data=f"live_del_{m['source']}"
        )] for m in monitors]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "🗑 **Remove Monitor**\n\nSelect which monitor to remove:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback.answer()

    elif action.startswith("live_del_"):
        source_str = action[len("live_del_"):]
        try: source = int(source_str)
        except: source = source_str
        await delete_live_monitor(user_id, source)
        if user_id in live_tasks and source in live_tasks[user_id]:
            live_tasks[user_id][source].cancel()
            del live_tasks[user_id][source]
        live_queues.pop((user_id, source), None)
        live_progress.pop(progress_key(user_id, source), None)
        await callback.answer("✅ Monitor removed!", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)

    # ── TOGGLE ──
    elif action == "live_toggle":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(
            f"{'🟢' if m['active'] else '⏸'} {m.get('source_title') or str(m['source'])}",
            callback_data=f"live_tog_{m['source']}"
        )] for m in monitors]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "⏸ **Pause / Resume Monitor**\n\nTap to toggle:",
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
                if user_id in live_tasks and source in live_tasks.get(user_id, {}):
                    live_tasks[user_id][source].cancel()
                    del live_tasks[user_id][source]
                await callback.answer("⏸ Monitor paused!", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)

    # ── SILENT ──
    elif action == "live_silent_menu":
        monitors = await get_live_monitors(user_id)
        if not monitors:
            await callback.answer("No monitors!", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(
            f"{'🔇 ON' if m.get('silent') else '🔔 OFF'} — {m.get('source_title') or str(m['source'])}",
            callback_data=f"live_siltog_{m['source']}"
        )] for m in monitors]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="live_refresh")])
        await callback.message.edit_text(
            "🔇 **Silent Mode** — Forward without notification\n\nTap to toggle per monitor:",
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
            await callback.answer(f"{'🔇 Silent ON' if new_silent else '🔔 Silent OFF'}", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)

    elif action == "live_upgrade":
        await callback.answer("Upgrade your plan to add more monitors!", show_alert=True)

    elif action == "live_refresh":
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)
        await callback.answer("🔄 Refreshed!")

    elif action == "live_close":
        try: await callback.message.delete()
        except: pass
        await callback.answer()

# ══════════════════════════════════════════════════
# SETUP INPUT HANDLER
# ══════════════════════════════════════════════════
async def handle_livebatch_input(client, message):
    user_id = message.from_user.id
    if user_id not in livebatch_states:
        return False

    state = livebatch_states[user_id]
    step = state["step"]
    text = (message.text or "").strip()

    if step == "SOURCE":
        try:
            source_id, _ = parse_channel_input(text)
        except ValueError:
            await message.reply_text(
                "❌ **Invalid Format**\n\n"
                "Send: `https://t.me/c/123456789/1`, `@channel`, or `-100ID`"
            )
            return True

        if isinstance(source_id, int) and await is_protected_channel(source_id):
            del livebatch_states[user_id]
            await message.reply_text("🔮 **Protected Channel!** Cannot monitor this channel.")
            return True

        # Try to fetch title via userbot
        source_title = str(source_id)
        try:
            session = await get_session(user_id)
            if session:
                ub = Client(f"tmp_title_{user_id}", api_id=API_ID, api_hash=API_HASH,
                            session_string=session, in_memory=True)
                await ub.start()
                try:
                    chat = await ub.get_chat(source_id)
                    source_title = chat.title or chat.first_name or str(source_id)
                finally:
                    await ub.stop()
        except: pass

        state["source"] = source_id
        state["source_title"] = source_title
        state["step"] = "DEST"

        await message.reply_text(
            f"✅ **Source Set!**\n\n"
            f"📡 **Channel:** `{source_title}`\n"
            f"🆔 **ID:** `{source_id}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Step 2/2 — Pick Destination(s):**\n\n"
            "Select which channel(s) to forward into.\n"
            "You can choose **multiple** channels at once!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Pick Destination Channels", callback_data="live_open_dest_picker")],
                [InlineKeyboardButton("❌ Cancel", callback_data="live_cancel_setup")],
            ])
        )
        return True

    elif step == "DEST":
        # DEST is now handled via channel picker — ignore stray text
        await message.reply_text(
            "⚠️ Please select destination channel(s) using the button below.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📡 Pick Destination", callback_data="live_open_dest_picker")]
            ])
        )
        return True

    return False

# ── Open destination picker for livebatch ──────────────────────────────
@Client.on_callback_query(filters.regex("^live_open_dest_picker$"))
async def live_open_dest_picker(client, callback):
    user_id = callback.from_user.id
    state = livebatch_states.get(user_id)
    if not state or state.get("step") != "DEST":
        await callback.answer("Session expired. Run /livebatch again.", show_alert=True)
        return

    from database import get_settings as _get_settings
    from plugins.channel_picker import open_channel_picker

    settings = await _get_settings(user_id)
    default_live = (settings or {}).get("default_live_channels", [])

    async def on_live_dest_confirmed(cl, cb, uid, selected_channels, extra):
        s = livebatch_states.get(uid, {})
        source_id    = s.get("source")
        source_title = s.get("source_title", str(source_id))
        if not source_id:
            try: await cb.message.edit_text("❌ Session expired. Run /livebatch again.")
            except: pass
            return

        # Save one monitor per selected destination
        for dest in selected_channels:
            await save_live_monitor(uid, source_id, dest)
            await update_live_monitor_meta(uid, source_id, source_title=source_title)
            await start_monitor_task(cl, uid, source_id, dest)

        livebatch_states.pop(uid, None)

        dest_list = "\n".join(f"  • `{d}`" for d in selected_channels)
        try:
            await cb.message.edit_text(
                "╔══════════════════════╗\n"
                "║  📡  MONITORS ACTIVE!  ║\n"
                "╚══════════════════════╝\n\n"
                f"📡 **Source:** `{source_title}`\n"
                f"📤 **Destinations ({len(selected_channels)}):**\n{dest_list}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ Every new post auto-forwarded to ALL selected channels!\n"
                "• Restricted? DL+Upload (max 2 GB)\n"
                "• Queue system — nothing ever missed!\n\n"
                "Use /livebatch → 📊 for real-time stats!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📡 Manage Monitors", callback_data="live_refresh")]
                ])
            )
        except: pass

    await open_channel_picker(
        client, callback.message, user_id,
        mode="live_dest",
        on_confirm=on_live_dest_confirmed,
        pre_selected=default_live,
        is_edit=True,
    )
    await callback.answer()

# ══════════════════════════════════════════════════
# MONITOR TASK ENGINE (Queue-based)
# ══════════════════════════════════════════════════
async def start_monitor_task(client, user_id, source_channel, dest_channel):
    if user_id not in live_tasks:
        live_tasks[user_id] = {}
    if source_channel in live_tasks[user_id]:
        live_tasks[user_id][source_channel].cancel()

    q = asyncio.Queue()
    live_queues[(user_id, source_channel)] = q
    key = init_progress(user_id, source_channel)

    task = asyncio.create_task(
        monitor_channel(client, user_id, source_channel, dest_channel, q, key)
    )
    live_tasks[user_id][source_channel] = task
    logger.info(f"Started live monitor: User {user_id}, Source {source_channel}")

async def monitor_channel(client, user_id, source_channel, dest_channel, q: asyncio.Queue, prog_key):
    """Runs userbot, enqueues messages, processes them one by one."""
    userbot = None
    try:
        session = await get_session(user_id)
        if not session:
            logger.error(f"No session for user {user_id}")
            return

        userbot = Client(
            f"live_{user_id}_{source_channel}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session, in_memory=True
        )
        await userbot.start()
        logger.info(f"Monitor userbot UP: {user_id}/{source_channel}")

        # Enqueue incoming messages instantly — NEVER miss!
        @userbot.on_message(filters.chat(source_channel) & ~filters.service)
        async def enqueue_handler(ub_client, msg):
            await q.put(msg)
            if prog_key in live_progress:
                live_progress[prog_key]["pending"] = q.qsize()

        # Process queue
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=60)
            except asyncio.TimeoutError:
                continue

            live_progress[prog_key]["pending"] = q.qsize()
            try:
                await process_live_message(
                    userbot, client, user_id, source_channel, dest_channel, msg, prog_key
                )
            except FloodWait as fw:
                logger.warning(f"Core FloodWait [{user_id}]: {fw.value}s restriction. Sleeping and re-queueing msg.")
                await asyncio.sleep(fw.value + 2)
                # Re-queue the message to ensure it is not skipped!
                await q.put(msg)
                
            q.task_done()

    except asyncio.CancelledError:
        logger.info(f"Monitor cancelled: {user_id}/{source_channel}")
    except Exception as e:
        logger.error(f"Monitor crash [{user_id}/{source_channel}]: {e}")
    finally:
        if userbot:
            try: await userbot.stop()
            except: pass

async def process_live_message(userbot, bot, user_id, source_channel, dest_channel, msg, prog_key):
    """Process a single queued message."""
    try:
        settings = await get_settings(user_id) or {}
        filters_cfg = settings.get("filters", {"all": True})
        caption_rules = settings.get("caption_rules", {})
        custom_thumb_id = settings.get("custom_thumbnail")

        monitors = await get_live_monitors(user_id)
        mon_cfg = next((m for m in monitors if str(m["source"]) == str(source_channel)), {})
        silent = mon_cfg.get("silent", False)

        # ── Filter check ──
        ok = False
        if filters_cfg.get("all"):
            ok = True
        elif msg.media:
            mt = msg.media
            if mt == enums.MessageMediaType.PHOTO    and filters_cfg.get("photo"):    ok = True
            elif mt == enums.MessageMediaType.VIDEO   and filters_cfg.get("video"):    ok = True
            elif mt == enums.MessageMediaType.DOCUMENT and filters_cfg.get("document"): ok = True
            elif mt == enums.MessageMediaType.AUDIO   and filters_cfg.get("audio"):    ok = True
            elif mt == enums.MessageMediaType.ANIMATION and filters_cfg.get("video"):  ok = True
            elif filters_cfg.get("media") and mt in [
                enums.MessageMediaType.PHOTO, enums.MessageMediaType.VIDEO,
                enums.MessageMediaType.DOCUMENT, enums.MessageMediaType.AUDIO,
                enums.MessageMediaType.VOICE, enums.MessageMediaType.ANIMATION
            ]: ok = True
        elif msg.text and filters_cfg.get("text"):
            ok = True
        if not ok:
            return

        # ── Caption ──
        raw_cap = msg.caption or (msg.text if not msg.media else "") or ""
        cap = raw_cap
        for rem in caption_rules.get("removals", []):
            cap = cap.replace(rem, "")
        for old, new in caption_rules.get("replacements", {}).items():
            cap = cap.replace(old, new)
        cap = cap.strip()
        p = caption_rules.get("prefix", "")
        s = caption_rules.get("suffix", "")
        if p: cap = f"{p}\n{cap}" if cap else p
        if s: cap = f"{cap}\n{s}" if cap else s
        if caption_rules.get("remove_caption"): cap = ""

        # ── Dest id ──
        try: d_id = int(dest_channel) if str(dest_channel).lstrip("-").isdigit() else dest_channel
        except: d_id = dest_channel

        # ── Try fast copy first ──
        live_progress[prog_key]["method"] = "fast_copy"
        forwarded = False
        try:
            await userbot.copy_message(
                chat_id=d_id, from_chat_id=source_channel,
                message_id=msg.id, caption=cap or None,
                disable_notification=silent
            )
            forwarded = True
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 2)
            try:
                await userbot.copy_message(
                    chat_id=d_id, from_chat_id=source_channel,
                    message_id=msg.id, caption=cap or None,
                    disable_notification=silent
                )
                forwarded = True
            except: pass
        except Exception as e:
            err = str(e)
            if not any(x in err for x in ["FORWARDS_RESTRICTED", "restricted", "FORWARD"]):
                live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
                return

        # ── DL+Upload fallback ──
        if not forwarded:
            live_progress[prog_key]["method"] = "dl_upload"
            file_size = get_file_size(msg)

            # 2 GB size check
            if file_size > MAX_DL_SIZE:
                live_progress[prog_key]["skipped"] = live_progress[prog_key].get("skipped", 0) + 1
                live_progress[prog_key]["current_file"] = f"⏭ Skipped: {fmt_size(file_size)} > 2GB"
                logger.info(f"Skipped large file: {fmt_size(file_size)} [{user_id}/{source_channel}]")
                return

            live_progress[prog_key]["current_size"] = file_size
            live_progress[prog_key]["downloaded_size"] = 0

            f_path = None
            thumb_path = None
            try:
                if msg.text:
                    await userbot.send_message(d_id, cap or msg.text, disable_notification=silent)
                    forwarded = True
                elif msg.media:
                    fname = getattr(getattr(msg, "document", None), "file_name", None) or \
                            getattr(getattr(msg, "video", None), "file_name", None) or \
                            f"file_{msg.id}"
                    live_progress[prog_key]["current_file"] = fname

                    try:
                        f_path = await userbot.download_media(msg)
                    except ValueError as ve:
                        if "0 B" in str(ve):
                            logger.warning(f"0B Error on live msg {msg.id}. Re-fetching.")
                            fresh_msg = await userbot.get_messages(source_channel, msg.id)
                            if fresh_msg and fresh_msg.media:
                                f_path = await userbot.download_media(fresh_msg)
                        else:
                            raise ve
                    if f_path:
                        live_progress[prog_key]["downloaded_size"] = os.path.getsize(f_path)

                    if not f_path:
                        live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
                        return

                    # Thumbnail
                    if custom_thumb_id:
                        try: thumb_path = await bot.download_media(custom_thumb_id)
                        except: pass
                    elif msg.video and msg.video.thumbs:
                        try: thumb_path = await userbot.download_media(msg.video.thumbs[0].file_id)
                        except: pass

                    kw = {"disable_notification": silent}
                    if msg.photo:
                        await userbot.send_photo(d_id, f_path, caption=cap or None, **kw)
                    elif msg.video:
                        await userbot.send_video(
                            d_id, f_path, caption=cap or None,
                            duration=msg.video.duration,
                            width=msg.video.width, height=msg.video.height,
                            thumb=thumb_path, **kw
                        )
                    elif msg.document:
                        await userbot.send_document(d_id, f_path, caption=cap or None, force_document=True, **kw)
                    elif msg.audio:
                        await userbot.send_audio(
                            d_id, f_path, caption=cap or None,
                            duration=msg.audio.duration,
                            performer=msg.audio.performer,
                            title=msg.audio.title, **kw
                        )
                    elif msg.voice:
                        await userbot.send_voice(d_id, f_path, caption=cap or None,
                                                  duration=msg.voice.duration, **kw)
                    elif msg.animation:
                        await userbot.send_animation(d_id, f_path, caption=cap or None, **kw)
                    elif msg.sticker:
                        await userbot.send_sticker(d_id, f_path, **kw)
                    else:
                        await userbot.send_document(d_id, f_path, caption=cap or None, **kw)
                    forwarded = True
            finally:
                live_progress[prog_key]["current_file"] = ""
                live_progress[prog_key]["current_size"] = 0
                live_progress[prog_key]["downloaded_size"] = 0
                live_progress[prog_key]["method"] = "idle"
                for pp in [f_path, thumb_path]:
                    if pp and os.path.exists(pp):
                        try: os.remove(pp)
                        except: pass

        if forwarded:
            live_progress[prog_key]["forwarded"] = live_progress[prog_key].get("forwarded", 0) + 1
            live_progress[prog_key]["last_update"] = time.time()
            await increment_live_stats(user_id, source_channel)
            await increment_channel_stat(user_id, dest_channel)
            logger.info(f"Live forwarded: {source_channel} → {dest_channel} [{user_id}]")

    except asyncio.CancelledError:
        raise
    except FloodWait as fw:
        raise fw  # Bubble up to monitor_channel queue loop to sleep and retry
    except Exception as e:
        if prog_key in live_progress:
            live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
        logger.error(f"process_live_message error [{user_id}/{source_channel}]: {e}")

# ══════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════
async def init_live_monitors(bot_client):
    try:
        all_monitors = await get_all_live_monitors()
        logger.info(f"Initializing {len(all_monitors)} active live monitors...")
        for m in all_monitors:
            await start_monitor_task(bot_client, m["user_id"], m["source"], m["dest"])
        logger.info("Live monitors initialized successfully!")
    except Exception as e:
        logger.error(f"Failed to initialize live monitors: {e}")
