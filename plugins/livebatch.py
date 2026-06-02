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
from plugins.text_cleaner import apply_text_clean
from config import API_ID, API_HASH, OWNER_ID

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════
MAX_DL_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB limit for DL+Upload

# ══════════════════════════════════════════════════
# STATE STORES
# ══════════════════════════════════════════════════
live_tasks    = {}   # user_id → {source → asyncio.Task}  (queue processor per source)
live_queues   = {}   # (user_id, source) → asyncio.Queue
live_progress = {}   # (user_id, source) → progress_dict
livebatch_states = {}       # user_id → setup step dict
live_filter_edit_state = {} # user_id → {"source": id, "filters": {...}}

# ── Shared userbot store ──
# ONE Client per user_id — shared across ALL monitors for that user.
# This is the critical design: multiple Pyrogram Client instances on the
# same session string cause Telegram to drop the older connection, which
# is why only the latest monitor received messages. Now we open exactly
# one connection per user and route messages to per-source queues.
_user_userbots = {}   # user_id → pyrogram.Client (running)
_user_ub_lock  = {}   # user_id → asyncio.Lock  (prevent double-start races)
_user_sources  = {}   # user_id → set of int source channel ids being monitored
_user_ub_handler_installed = set()  # user_ids whose fan-out handler is installed

def _norm_source(source_channel):
    """Normalize source_channel to int where possible.
    Private channels are always int (-100xxx). Public @username stays str.
    This ensures live_queues keys & _user_sources entries match msg.chat.id (always int).
    """
    if isinstance(source_channel, str):
        try:
            return int(source_channel)
        except ValueError:
            return source_channel  # keep as @username string
    return source_channel

def get_shared_userbot(user_id: int):
    """Return the running shared userbot for user_id, or None.
    Exposed so copy_manager can REUSE it instead of creating a second
    Pyrogram session (which would kill LiveBatch update delivery).
    """
    return _user_userbots.get(user_id)

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

# ── Per-monitor filter helpers ──────────────────────────────────
_FILTER_PAIRS = [
    ("photo",    "🖼 Photos"),
    ("video",    "📹 Videos"),
    ("document", "📂 Files"),
    ("audio",    "🎵 Audio"),
    ("text",     "📝 Text"),
    ("media",    "📱 Any Media"),
]

def _toggle_filter(filters: dict, key: str) -> dict:
    f = dict(filters)
    if key == "all":
        f["all"] = not f.get("all", False)
        if f["all"]:
            for k in ["photo","video","document","audio","text","media"]:
                f[k] = False
    else:
        f[key] = not f.get(key, False)
        if f[key]:          # turning ON a specific type → disable "all"
            f["all"] = False
    return f

def _get_filter_label(filters: dict) -> str:
    if not filters or filters.get("all"):
        return "📋 All Content"
    parts = []
    for key, label in _FILTER_PAIRS:
        if filters.get(key): parts.append(label)
    return " | ".join(parts) if parts else "📋 All Content"

def _build_filter_text(source_title: str = "", is_edit: bool = False) -> str:
    step = "Edit" if is_edit else "Step 3/3 — Set"
    src  = f"📡 **Monitor:** `{source_title}`\n\n" if source_title else ""
    return (
        f"🔍 **{step} Filters**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{src}"
        "Choose which content to forward:\n"
        "• **All Content** — forward everything (default)\n"
        "• Turn **All** OFF, then pick specific types\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 _Takes effect on all future messages._"
    )

def _build_filter_kb(filters: dict, confirm_cb: str,
                     tog_prefix: str = "live_ftog_",
                     back_cb: str = None) -> InlineKeyboardMarkup:
    tick = "✅"; cross = "❌"
    is_all = filters.get("all", False)
    rows = [[
        InlineKeyboardButton(
            f"{tick if is_all else cross} 📋 All Content",
            callback_data=f"{tog_prefix}all"
        )
    ]]
    for i in range(0, len(_FILTER_PAIRS), 2):
        row = []
        for key, label in _FILTER_PAIRS[i:i+2]:
            active = is_all or filters.get(key, False)
            icon = "✅" if active else "❌"
            row.append(InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=f"{tog_prefix}{key}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("✅ Save Filters", callback_data=confirm_cb)])
    if back_cb:
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)

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

    # ── DEST PICKER (must be here — the ^live_ regex catches it before the
    #    standalone handler can fire, so we route it explicitly) ──
    elif action == "live_open_dest_picker":
        await _handle_live_open_dest_picker(client, callback)
        return

    # ── FILTER SETUP (step 3 of new monitor setup) ──
    elif action.startswith("live_ftog_"):
        key   = action[len("live_ftog_"):]
        state = livebatch_states.get(user_id, {})
        if state.get("step") != "FILTERS":
            await callback.answer("Session expired. Run /livebatch again.", show_alert=True)
            return
        state["filters"] = _toggle_filter(state.get("filters", {"all": True}), key)
        kb   = _build_filter_kb(state["filters"], "live_fconfirm")
        text = _build_filter_text(state.get("source_title", ""))
        try: await callback.message.edit_text(text, reply_markup=kb)
        except: pass
        await callback.answer()

    elif action == "live_fconfirm":
        state = livebatch_states.pop(user_id, {})
        if state.get("step") != "FILTERS":
            await callback.answer("Session expired.", show_alert=True)
            return
        source_id    = state["source"]
        source_title = state.get("source_title", str(source_id))
        dests        = state.get("dests", [])
        filters      = state.get("filters", {"all": True})
        # Save ALL dests in ONE monitor document, start ONE task
        await save_live_monitor(user_id, source_id, dests)
        await update_live_monitor_meta(user_id, source_id, source_title=source_title, filters=filters)
        await start_monitor_task(client, user_id, source_id, dests)
        dest_list   = "\n".join(f"  • `{d}`" for d in dests)
        filter_disp = _get_filter_label(filters)
        try:
            await callback.message.edit_text(
                "╬══════════════════════╬\n"
                "║  📡  MONITORS ACTIVE!  ║\n"
                "╚══════════════════════╝\n\n"
                f"📡 **Source:** `{source_title}`\n"
                f"📬 **Destinations ({len(dests)}):**\n{dest_list}\n"
                f"🔍 **Filters:** {filter_disp}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ Every new post auto-forwarded!\n"
                "Use /livebatch → 📊 for stats.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📡 Manage Monitors", callback_data="live_refresh")]
                ])
            )
        except: pass
        await callback.answer("✅ Monitor started!")

    # ── FILTER EDIT (for existing monitors) ──
    elif action.startswith("live_editfilter_"):
        source_str = action[len("live_editfilter_"):]
        try: source = int(source_str)
        except: source = source_str
        monitors = await get_live_monitors(user_id)
        m = next((x for x in monitors if str(x["source"]) == str(source)), None)
        if not m:
            await callback.answer("Monitor not found!", show_alert=True)
            return
        cur_filters = m.get("filters") or {"all": True}
        live_filter_edit_state[user_id] = {"source": source, "filters": dict(cur_filters)}
        source_title = m.get("source_title", str(source))
        text = _build_filter_text(source_title, is_edit=True)
        kb   = _build_filter_kb(cur_filters, "live_efconfirm",
                                tog_prefix="live_eftog_",
                                back_cb=f"live_mon_stat_{source}")
        try: await callback.message.edit_text(text, reply_markup=kb)
        except: pass
        await callback.answer()

    elif action.startswith("live_eftog_"):
        key        = action[len("live_eftog_"):]
        edit_state = live_filter_edit_state.get(user_id)
        if not edit_state:
            await callback.answer("Session expired.", show_alert=True)
            return
        edit_state["filters"] = _toggle_filter(edit_state["filters"], key)
        source = edit_state["source"]
        monitors = await get_live_monitors(user_id)
        m = next((x for x in monitors if str(x["source"]) == str(source)), None)
        source_title = m.get("source_title", str(source)) if m else str(source)
        text = _build_filter_text(source_title, is_edit=True)
        kb   = _build_filter_kb(edit_state["filters"], "live_efconfirm",
                                tog_prefix="live_eftog_",
                                back_cb=f"live_mon_stat_{source}")
        try: await callback.message.edit_text(text, reply_markup=kb)
        except: pass
        await callback.answer()

    elif action == "live_efconfirm":
        edit_state = live_filter_edit_state.pop(user_id, None)
        if not edit_state:
            await callback.answer("Session expired.", show_alert=True)
            return
        source  = edit_state["source"]
        filters = edit_state["filters"]
        await update_live_monitor_meta(user_id, source, filters=filters)
        await callback.answer(f"✅ Filters saved: {_get_filter_label(filters)}", show_alert=True)
        await show_livebatch_menu(callback.message, user_id, limit, is_edit=True)

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
        task_obj = live_tasks.get(user_id, {}).get(source)
        task_alive = task_obj is not None and not task_obj.done()
        ub_alive = user_id in _user_userbots and _user_userbots[user_id].is_connected
        engine_status = "✅ Engine Running" if task_alive else "💤 Engine Stopped"
        ub_status = "🔗 Connected" if ub_alive else "⚠️ Disconnected"
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
            f"📤 **Destinations:** `{len(m['dest'])}` channel(s)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔋 **Status:** {status}\n"
            f"⚙️ **Engine:** {engine_status}\n"
            f"🌐 **Userbot:** {ub_status}\n"
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
            f"{dl_bar}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 **Filters:** {_get_filter_label(m.get('filters'))}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Stats",    callback_data=f"live_mon_stat_{source}")],
            [InlineKeyboardButton("🔍 Edit Filters",     callback_data=f"live_editfilter_{source}")],
            [InlineKeyboardButton("⬅️ Back to Hub",     callback_data="live_refresh")],
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
        # Cancel queue-processor task
        if user_id in live_tasks and source in live_tasks[user_id]:
            live_tasks[user_id][source].cancel()
            del live_tasks[user_id][source]
        live_queues.pop((user_id, source), None)
        live_progress.pop(progress_key(user_id, source), None)
        # Remove from shared-userbot source registry
        if user_id in _user_sources:
            _user_sources[user_id].discard(source)
        asyncio.create_task(_stop_userbot_if_idle(user_id))
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
                # Pause: cancel queue processor and remove from shared routing
                if user_id in live_tasks and source in live_tasks.get(user_id, {}):
                    live_tasks[user_id][source].cancel()
                    del live_tasks[user_id][source]
                if user_id in _user_sources:
                    _user_sources[user_id].discard(source)
                asyncio.create_task(_stop_userbot_if_idle(user_id))
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
# NOTE: NOT decorated — routed via livebatch_callback_handler to avoid
# the ^live_ regex consuming it before this handler can fire.
async def _handle_live_open_dest_picker(client, callback):
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

        # Transition to FILTERS step (step 3/3) — do NOT save yet
        s["step"]    = "FILTERS"
        s["dests"]   = selected_channels
        s["filters"] = {"all": True}   # sensible default
        livebatch_states[uid] = s

        text = _build_filter_text(source_title)
        kb   = _build_filter_kb(s["filters"], "live_fconfirm")
        try:
            await cb.message.edit_text(text, reply_markup=kb)
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
# ══════════════════════════════════════════════════
# SHARED USERBOT MANAGER
# ══════════════════════════════════════════════════
async def _get_or_start_userbot(user_id: int) -> "Client | None":
    """
    Return the running shared userbot for this user.
    If it does not exist yet, create & start it.
    Only ONE Client instance per user — shared across ALL monitors.
    """
    if user_id not in _user_ub_lock:
        _user_ub_lock[user_id] = asyncio.Lock()

    async with _user_ub_lock[user_id]:
        existing = _user_userbots.get(user_id)
        if existing is not None:
            # Check the client is still alive
            try:
                if existing.is_connected:
                    return existing
            except Exception:
                pass
            # Stale — clean up and recreate
            try:
                await existing.stop()
            except Exception:
                pass
            _user_userbots.pop(user_id, None)

        session = await get_session(user_id)
        if not session:
            logger.error(f"[SharedUB] No session for user {user_id}")
            return None

        ub = Client(
            f"live_shared_{user_id}",
            api_id=API_ID, api_hash=API_HASH,
            session_string=session, in_memory=True
        )
        await ub.start()
        _user_userbots[user_id] = ub
        _user_sources[user_id]  = set()
        _user_ub_handler_installed.discard(user_id)  # Reset so handler is re-installed
        logger.info(f"[SharedUB] Started shared userbot for user {user_id}")
        return ub


async def _register_source_on_userbot(ub: "Client", user_id: int, source_channel):
    """
    Register a new source channel on the shared userbot.
    source_channel must already be normalized (int for private, str for public).
    Handler is installed exactly once per userbot lifecycle and fans-out
    to all per-source queues dynamically.
    """
    sources = _user_sources.setdefault(user_id, set())
    if source_channel in sources:
        return   # Already registered

    sources.add(source_channel)
    logger.info(f"[SharedUB] Registered source {source_channel} for user {user_id}. Total: {len(sources)}")

    if user_id not in _user_ub_handler_installed:
        # Install the shared fan-out handler exactly once per userbot lifecycle
        _user_ub_handler_installed.add(user_id)

        @ub.on_message(~filters.service)
        async def _shared_enqueue(ub_client, msg):
            src = getattr(msg, "chat", None)
            if not src:
                return
            # msg.chat.id is ALWAYS int from Pyrogram — sources set also holds int
            chat_id = src.id

            monitored = _user_sources.get(user_id, set())
            if chat_id not in monitored:
                # Also try string form for @username public channels
                if f"@{getattr(src, 'username', '')}" not in monitored:
                    return
                chat_id = f"@{src.username}"

            q = live_queues.get((user_id, chat_id))
            if q is None:
                return

            await q.put(msg)
            pk = progress_key(user_id, chat_id)
            if pk in live_progress:
                live_progress[pk]["pending"] = q.qsize()

        logger.info(f"[SharedUB] Installed fan-out handler for user {user_id}")
    else:
        logger.info(f"[SharedUB] Source {source_channel} added to existing handler (user {user_id})")


async def _stop_userbot_if_idle(user_id: int):
    """Stop the shared userbot if no more sources are monitored."""
    if _user_sources.get(user_id):
        return  # Still has active sources
    ub = _user_userbots.pop(user_id, None)
    _user_ub_handler_installed.discard(user_id)
    if ub:
        try:
            await ub.stop()
        except Exception:
            pass
        logger.info(f"[SharedUB] Stopped idle shared userbot for user {user_id}")


# ══════════════════════════════════════════════════
# MONITOR TASK ENGINE (Queue-based, Shared Userbot)
# ══════════════════════════════════════════════════
async def start_monitor_task(client, user_id, source_channel, dest_channels):
    """
    Start (or restart) the queue-processor task for one source channel.
    The shared userbot is obtained/started once per user and reused.
    source_channel is normalized to int (private channels) or str (@username)
    so it always matches msg.chat.id from Pyrogram.
    """
    # ── Normalize source type so all keys are consistent ──────────────
    source_channel = _norm_source(source_channel)

    if not isinstance(dest_channels, list):
        dest_channels = [dest_channels]
    if user_id not in live_tasks:
        live_tasks[user_id] = {}

    # Cancel any existing processor task for this source
    old_task = live_tasks[user_id].get(source_channel)
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=3)
        except Exception:
            pass

    # Create a fresh queue for this source (keyed by normalized int/str)
    q = asyncio.Queue()
    live_queues[(user_id, source_channel)] = q
    key = init_progress(user_id, source_channel)

    # Ensure the shared userbot is running and this source is registered
    ub = await _get_or_start_userbot(user_id)
    if ub is None:
        logger.error(f"[Monitor] Cannot start — no userbot for user {user_id}")
        return
    await _register_source_on_userbot(ub, user_id, source_channel)

    # Spawn the queue-processor coroutine
    task = asyncio.create_task(
        _queue_processor(ub, client, user_id, source_channel, dest_channels, q, key)
    )
    live_tasks[user_id][source_channel] = task
    logger.info(f"[Monitor] Started: user={user_id} source={source_channel} dests={len(dest_channels)}")


async def _queue_processor(userbot, bot, user_id, source_channel, dest_channels,
                            q: asyncio.Queue, prog_key):
    """
    Pure queue consumer — no userbot lifecycle here.
    The shared userbot is managed by _get_or_start_userbot / _stop_userbot_if_idle.
    """
    logger.info(f"[QueueProc] Running: user={user_id} source={source_channel}")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=60)
            except asyncio.TimeoutError:
                # Keep-alive: check if userbot is still connected
                ub = _user_userbots.get(user_id)
                if ub is None or not ub.is_connected:
                    logger.warning(f"[QueueProc] Userbot gone for user {user_id}, restarting...")
                    new_ub = await _get_or_start_userbot(user_id)
                    if new_ub:
                        await _register_source_on_userbot(new_ub, user_id, source_channel)
                        userbot = new_ub
                continue

            if prog_key in live_progress:
                live_progress[prog_key]["pending"] = q.qsize()

            try:
                # Re-fetch the live userbot reference in case it was restarted
                current_ub = _user_userbots.get(user_id, userbot)
                await process_live_message(
                    current_ub, bot, user_id, source_channel, dest_channels, msg, prog_key
                )
            except FloodWait as fw:
                logger.warning(f"[QueueProc] FloodWait {fw.value}s [{user_id}/{source_channel}]")
                await asyncio.sleep(fw.value + 2)
                # Re-queue so message is NOT lost
                await q.put(msg)
            except ValueError as ve:
                if str(ve) == "FLOOD_WAIT_0B":
                    logger.warning(f"[QueueProc] FLOOD_WAIT_0B [{user_id}] — sleeping 50 min")
                    await asyncio.sleep(3000)
                    await q.put(msg)
                else:
                    logger.error(f"[QueueProc] ValueError [{user_id}/{source_channel}]: {ve}")

            q.task_done()

    except asyncio.CancelledError:
        logger.info(f"[QueueProc] Cancelled: user={user_id} source={source_channel}")
    except Exception as e:
        logger.error(f"[QueueProc] Crash [{user_id}/{source_channel}]: {e}")

async def process_live_message(userbot, bot, user_id, source_channel, dest_channels, msg, prog_key):
    """Process a single queued message — forwards to ALL dest channels."""
    if not isinstance(dest_channels, list):
        dest_channels = [dest_channels]
    try:
        settings = await get_settings(user_id) or {}
        caption_rules   = settings.get("caption_rules", {})
        text_clean      = settings.get("text_clean", {})
        custom_thumb_id = settings.get("custom_thumbnail")

        monitors = await get_live_monitors(user_id)
        mon_cfg  = next((m for m in monitors if str(m["source"]) == str(source_channel)), {})
        silent   = mon_cfg.get("silent", False)

        # Per-monitor filters (fallback to global settings)
        filters_cfg = mon_cfg.get("filters") or settings.get("filters", {"all": True})

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
        if cap and text_clean:
            cap = apply_text_clean(cap, text_clean, caption_rules)

        # ── Resolve dest IDs ──
        dest_ids = []
        for dc in dest_channels:
            try: dest_ids.append(int(dc) if str(dc).lstrip("-").isdigit() else dc)
            except: dest_ids.append(dc)

        # ── Try fast copy to ALL dests ──
        live_progress[prog_key]["method"] = "fast_copy"
        forwarded = False
        needs_dl  = False   # becomes True if source is forward-restricted

        for d_id in dest_ids:
            if needs_dl:
                break   # source is restricted — no point trying more copies
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
                except Exception:
                    needs_dl = True
            except Exception as e:
                err = str(e)
                if any(x in err for x in ["FORWARDS_RESTRICTED", "restricted", "FORWARD"]):
                    needs_dl = True
                else:
                    live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1

        # ── DL+Upload fallback (download ONCE, send to all restricted dests) ──
        if needs_dl:
            live_progress[prog_key]["method"] = "dl_upload"
            file_size = get_file_size(msg)

            if file_size > MAX_DL_SIZE:
                live_progress[prog_key]["skipped"] = live_progress[prog_key].get("skipped", 0) + 1
                live_progress[prog_key]["current_file"] = f"⏭ Skipped: {fmt_size(file_size)} >2GB"
                logger.info(f"Skipped large file: {fmt_size(file_size)} [{user_id}/{source_channel}]")
            else:
                live_progress[prog_key]["current_size"] = file_size
                live_progress[prog_key]["downloaded_size"] = 0

                f_path = None
                thumb_path = None
                try:
                    if msg.text:
                        for d_id in dest_ids:
                            try:
                                await userbot.send_message(d_id, cap or msg.text, disable_notification=silent)
                                forwarded = True
                            except Exception as e:
                                live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
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
                                await asyncio.sleep(2)
                                fresh_msg = await userbot.get_messages(source_channel, msg.id)
                                if fresh_msg and fresh_msg.media:
                                    try:
                                        f_path = await userbot.download_media(fresh_msg)
                                    except ValueError as double_ve:
                                        if "0 B" in str(double_ve):
                                            logger.warning("Double 0B err! FloodWait blocking LiveBatch download stream.")
                                            raise ValueError("FLOOD_WAIT_0B")
                                        raise double_ve
                            else:
                                raise ve

                        if f_path:
                            live_progress[prog_key]["downloaded_size"] = os.path.getsize(f_path)

                        if not f_path:
                            live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
                        else:
                            # Thumbnail built once, reused for all dests
                            if custom_thumb_id:
                                try: thumb_path = await bot.download_media(custom_thumb_id)
                                except: pass
                            elif msg.video and msg.video.thumbs:
                                try: thumb_path = await userbot.download_media(msg.video.thumbs[0].file_id)
                                except: pass

                            kw = {"disable_notification": silent}
                            for d_id in dest_ids:
                                try:
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
                                except Exception as send_err:
                                    live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
                                    logger.error(f"DL+Upload send error [{user_id}] to {d_id}: {send_err}")
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
            for dc in dest_channels:
                await increment_channel_stat(user_id, dc)
            logger.info(f"Live forwarded: {source_channel} → {dest_channels} [{user_id}]")

    except asyncio.CancelledError:
        raise
    except FloodWait as fw:
        raise fw  # Bubble up to monitor_channel queue loop to sleep and retry
    except ValueError as ve:
        if str(ve) == "FLOOD_WAIT_0B":
            raise ve  # Bubble up explicitly to monitor_channel
        if prog_key in live_progress:
            live_progress[prog_key]["errors"] = live_progress[prog_key].get("errors", 0) + 1
        logger.error(f"process_live_message error [{user_id}/{source_channel}]: {ve}")
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
