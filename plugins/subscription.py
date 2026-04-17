from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import get_subscription, set_subscription, update_user_task, send_log_api, get_db
from config import OWNER_ID, FORCE_CHANNEL_ID
import time
import datetime
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# PLAN DEFINITIONS
# ═══════════════════════════════════════════════════════════
PLANS = {
    "free": {
        "name": "🆓 Free Plan",
        "emoji": "🆓",
        "price": "FREE",
        "task_limit": 3,           # 3 tasks total (lifetime)
        "forward_limit": 1000,     # Fast copy
        "dl_limit": 100,           # Download+Upload (restricted)
        "duration": 0,
        "live_monitor_limit": 0,
        "one_time": False,
        "color": "⚪",
        "badge": "FREE",
        "self_activate": False,
    },
    "trial": {
        "name": "🎁 Trial Plan",
        "emoji": "🎁",
        "price": "FREE (1x Only)",
        "task_limit": 1,
        "forward_limit": 20000,
        "dl_limit": 5000,
        "duration": 86400,         # 24 hours
        "live_monitor_limit": 0,
        "one_time": True,
        "color": "🟢",
        "badge": "FREE TRIAL",
        "self_activate": True,     # User can activate themselves!
    },
    "daily_39": {
        "name": "⚡ Daily Pass",
        "emoji": "⚡",
        "price": "₹39",
        "task_limit": 5,
        "forward_limit": 100000,
        "dl_limit": 19999,
        "duration": 86400,
        "live_monitor_limit": 2,
        "one_time": False,
        "color": "🔵",
        "badge": "POPULAR",
        "self_activate": False,
    },
    "monthly_259": {
        "name": "💎 Monthly Pro",
        "emoji": "💎",
        "price": "₹259",
        "task_limit": 50,
        "forward_limit": 1000000,
        "dl_limit": 200000,
        "duration": 2592000,
        "live_monitor_limit": 5,
        "one_time": False,
        "color": "🟣",
        "badge": "BEST VALUE",
        "self_activate": False,
    },
    "ultra_389": {
        "name": "🚀 Ultra Pass",
        "emoji": "🚀",
        "price": "₹389",
        "task_limit": float('inf'),
        "forward_limit": float('inf'),
        "dl_limit": 1000000,
        "duration": 259200,
        "live_monitor_limit": 15,
        "one_time": False,
        "color": "🟠",
        "badge": "POWER USER",
        "self_activate": False,
    },
    "lifetime_2999": {
        "name": "♾️ Lifetime",
        "emoji": "♾️",
        "price": "₹2999",
        "task_limit": float('inf'),
        "forward_limit": float('inf'),
        "dl_limit": float('inf'),
        "duration": 0,
        "live_monitor_limit": 30,
        "one_time": False,
        "color": "🔴",
        "badge": "ULTIMATE",
        "self_activate": False,
    },
}

# Legacy key migration
LEGACY_PLAN_MAP = {
    "day_19": "daily_39",
    "month_199": "monthly_259",
    "unlimited_299": "ultra_389",
}

# Ordered for buttons display
PLAN_ORDER = ["trial", "daily_39", "monthly_259", "ultra_389", "lifetime_2999"]

def fmt_num(val):
    if val == float('inf'): return "∞ Unlimited"
    return f"{int(val):,}"

def fmt_tasks(val):
    if val == float('inf'): return "∞ Unlimited"
    return str(int(val))

# ═══════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════════════════════
async def has_used_trial(user_id: int) -> bool:
    database = await get_db()
    rec = await database.trial_used.find_one({"_id": user_id})
    return rec is not None

async def mark_trial_used(user_id: int):
    database = await get_db()
    await database.trial_used.update_one({"_id": user_id}, {"$set": {"used": True, "ts": time.time()}}, upsert=True)

async def get_resolved_plan(user_id):
    """Returns (plan_key, plan_dict, tasks_done, expiry) — always normalized."""
    sub = await get_subscription(user_id)
    if not sub:
        return "free", PLANS["free"], 0, 0

    plan_key = sub.get("plan_type", "free")
    plan_key = LEGACY_PLAN_MAP.get(plan_key, plan_key)
    if plan_key not in PLANS:
        plan_key = "free"

    expiry = sub.get("expiry_date", 0)
    tasks_done = sub.get("tasks_done", 0)
    now = time.time()

    # Auto-expire non-lifetime plans
    if plan_key not in ("free", "lifetime_2999") and expiry > 0 and now > expiry:
        await set_subscription(user_id, "free", 0)
        plan_key = "free"
        expiry = 0

    return plan_key, PLANS[plan_key], tasks_done, expiry

# ═══════════════════════════════════════════════════════════
# FORCE SUBSCRIBE CHECK
# ═══════════════════════════════════════════════════════════
async def check_force_sub(client, message):
    user_id = message.from_user.id
    if user_id == int(OWNER_ID):
        return True
    INVITE_LINK = "https://t.me/Univora88"
    try:
        member = await client.get_chat_member(FORCE_CHANNEL_ID, user_id)
        VALID = [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        if member.status in VALID:
            return True
        raise UserNotParticipant
    except UserNotParticipant:
        await message.reply_text(
            "⚠️ **Access Verification Required**\n\n"
            "To use **ExtractX**, you must join our official channel.\n"
            "1. Join the channel below.\n"
            "2. Click **Check Access** to proceed.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Official Channel", url=INVITE_LINK)],
                [InlineKeyboardButton("🔄 Check Access", url=f"https://t.me/{client.me.username}?start=check")]
            ])
        )
        return False
    except Exception as e:
        logger.warning(f"Force sub check error for {user_id}: {e}")
        return True

# ═══════════════════════════════════════════════════════════
# ACCESS CHECK ENGINE
# ═══════════════════════════════════════════════════════════
async def check_user_access(user_id):
    """Returns: (is_allowed, reason_message, forward_limit, tasks_remaining)"""
    if user_id == int(OWNER_ID):
        return True, "Owner Access", float('inf'), "Unlimited"

    plan_key, plan, tasks, expiry = await get_resolved_plan(user_id)
    task_limit = plan["task_limit"]
    forward_limit = plan["forward_limit"]

    if task_limit != float('inf') and tasks >= task_limit:
        return False, (
            f"⚠️ **Task Limit Reached!**\n\n"
            f"Your **{plan['name']}** allows `{fmt_tasks(task_limit)}` tasks.\n"
            f"You've used all of them! ❌\n\n"
            f"📲 Use /showplan to upgrade or activate your **Free Trial!**"
        ), forward_limit, 0

    remaining = (task_limit - tasks) if task_limit != float('inf') else "Unlimited"
    return True, "Access Granted", forward_limit, remaining

async def record_task_use(user_id):
    if user_id == int(OWNER_ID): return
    await update_user_task(user_id, 1)

# ═══════════════════════════════════════════════════════════
# UI BUILDERS
# ═══════════════════════════════════════════════════════════
def make_progress_bar(used, total, length=10):
    if total == float('inf') or total == 0:
        return "🟩" * length
    ratio = min(used / total, 1.0)
    filled = int(length * ratio)
    if ratio >= 0.9:
        bar_char = "🟥"
    elif ratio >= 0.6:
        bar_char = "🟨"
    else:
        bar_char = "🟩"
    return bar_char * filled + "⬜" * (length - filled)

def build_status_card(first_name, user_id, plan_key, plan, tasks_done, expiry):
    task_limit = plan["task_limit"]
    fwd_limit = plan["forward_limit"]
    dl_limit = plan["dl_limit"]
    live_limit = plan["live_monitor_limit"]

    # Task bar
    task_bar = make_progress_bar(tasks_done, task_limit)
    tasks_str = f"{tasks_done}/{fmt_tasks(task_limit)}"

    # Expiry
    now = time.time()
    if expiry > 0:
        exp_dt = datetime.datetime.fromtimestamp(expiry)
        exp_str = exp_dt.strftime("%-d %b %Y • %-I:%M %p")
        remaining_secs = max(0, expiry - now)
        remaining_days = int(remaining_secs // 86400)
        remaining_hrs = int((remaining_secs % 86400) // 3600)
        remaining_mins = int((remaining_secs % 3600) // 60)
        if remaining_days > 0:
            time_left = f"⏱ {remaining_days}d {remaining_hrs}h left"
        elif remaining_hrs > 0:
            time_left = f"⏱ {remaining_hrs}h {remaining_mins}m left"
        else:
            time_left = f"⏱ {remaining_mins}m left — Expiring soon!"
    elif plan_key == "lifetime_2999":
        exp_str = "Never Expires ♾️"
        time_left = "Forever"
    elif plan_key in ("free", "trial"):
        exp_str = "No fixed expiry"
        time_left = "Based on usage"
    else:
        exp_str = "—"
        time_left = "—"

    text = (
        f"╔══════════════════════╗\n"
        f"║    📊  PLAN STATUS    ║\n"
        f"╚══════════════════════╝\n\n"
        f"👤 **{first_name}**  •  `{user_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{plan['color']} **Plan:** {plan['name']}\n"
        f"🏷️ **Badge:** `{plan['badge']}`\n"
        f"💰 **Price:** `{plan['price']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **Usage Stats:**\n"
        f"  {task_bar}  `{tasks_str}` tasks\n"
        f"  🔗 Fast Copy: `{fmt_num(fwd_limit)}` files/task\n"
        f"  📦 DL+Upload: `{fmt_num(dl_limit)}` files/task\n"
        f"  📡 Live Monitors: `{live_limit if live_limit else 'None'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ **Valid Till:** `{exp_str}`\n"
        f"🕐 **Time Left:** `{time_left}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⬇️ **Choose a plan for details:**"
    )
    return text

async def build_plan_keyboard(user_id):
    """Build smartplan keyboard — trial button is green/red based on usage."""
    trial_used = await has_used_trial(user_id)

    buttons = []

    # Trial button — self-activatable
    if trial_used:
        trial_btn = InlineKeyboardButton("🔴 Trial — Already Used", callback_data="trial_used_notice")
    else:
        trial_btn = InlineKeyboardButton("🟢 Activate FREE Trial ✨", callback_data="activate_trial")
    buttons.append([trial_btn])

    # Paid plans row 1
    buttons.append([
        InlineKeyboardButton("⚡ Daily — ₹39", callback_data="plan_info:daily_39"),
        InlineKeyboardButton("💎 Monthly — ₹259", callback_data="plan_info:monthly_259"),
    ])
    # Paid plans row 2
    buttons.append([
        InlineKeyboardButton("🚀 Ultra — ₹389", callback_data="plan_info:ultra_389"),
        InlineKeyboardButton("♾️ Lifetime — ₹2999", callback_data="plan_info:lifetime_2999"),
    ])
    # Upgrade CTA
    buttons.append([
        InlineKeyboardButton("📲 Buy Premium", url="https://t.me/Univora_Support"),
        InlineKeyboardButton("🔄 Refresh", callback_data="show_plans_back"),
    ])
    return InlineKeyboardMarkup(buttons)

def build_plan_detail(plan_key):
    plan = PLANS[plan_key]
    fwd = fmt_num(plan['forward_limit'])
    dl = fmt_num(plan['dl_limit'])
    tasks = fmt_tasks(plan['task_limit'])
    live = str(plan['live_monitor_limit']) if plan['live_monitor_limit'] > 0 else "❌ Not Available"

    dur = plan['duration']
    if dur == 0:
        validity = "**Forever ♾️**" if plan_key == "lifetime_2999" else "**Free — No Expiry**"
    elif dur == 86400:
        validity = "**24 Hours**"
    elif dur == 259200:
        validity = "**3 Days**"
    elif dur == 2592000:
        validity = "**30 Days**"
    else:
        validity = f"**{int(dur // 86400)} Days**"

    # Feature comparison highlights
    vs_free_fwd = int(plan['forward_limit']) if plan['forward_limit'] != float('inf') else "∞"
    vs_free_dl = int(plan['dl_limit']) if plan['dl_limit'] != float('inf') else "∞"

    trial_note = "\n\n⚠️ _One-time activation — only 1 per account_" if plan.get("one_time") else ""

    text = (
        f"╔══════════════════════╗\n"
        f"║  {plan['emoji']}  {plan['name'].upper()}\n"
        f"╚══════════════════════╝\n\n"
        f"💰 **Price:** `{plan['price']}`\n"
        f"⏳ **Validity:** {validity}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **Plan Limits:**\n\n"
        f"  🔢 Total Tasks: `{tasks}`\n"
        f"  🔗 Fast Copy: `{fwd}` files\n"
        f"     _↳ Forwarded channel content_\n"
        f"  📦 DL+Upload: `{dl}` files\n"
        f"     _↳ Restricted / protected content_\n"
        f"  📡 Live Monitors: `{live}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ **All Features Included:**\n"
        f"  • 🔓 Private & restricted channel bypass\n"
        f"  • 🎯 Smart filters (Video/Doc/Photo/Audio)\n"
        f"  • ✏️ Caption Edit, Prefix/Suffix, Replace\n"
        f"  • 🖼️ Custom Thumbnail Override\n"
        f"  • 📤 Multi-destination forwarding\n"
        f"  • ⚡ Server-side blazing-fast copy\n"
        f"  • 📡 Live auto-forward monitor{trial_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📲 **Contact admin to activate!**"
    )
    return text

# ═══════════════════════════════════════════════════════════
# /showplan COMMAND
# ═══════════════════════════════════════════════════════════
@Client.on_message(filters.command(["showplan", "myplan", "plan"]))
async def show_plan(client, message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "User"

    # Owner special
    if user_id == int(OWNER_ID):
        await message.reply_text(
            "╔══════════════════════╗\n"
            "║  👑  OWNER GOD MODE  👑  ║\n"
            "╚══════════════════════╝\n\n"
            f"👤 **{first_name}**  •  `{user_id}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔴 **Plan:** `God Mode — Unlimited`\n"
            "⚡ **Tasks:** `∞`\n"
            "🔗 **Fast Copy:** `∞`\n"
            "📦 **DL+Upload:** `∞`\n"
            "📡 **Live Monitors:** `∞`\n"
            "⏳ **Validity:** `Forever`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ _You have complete control over ExtractX._"
        )
        return

    plan_key, plan, tasks_done, expiry = await get_resolved_plan(user_id)
    text = build_status_card(first_name, user_id, plan_key, plan, tasks_done, expiry)
    kb = await build_plan_keyboard(user_id)
    await message.reply_text(text, reply_markup=kb)

    # Silent log
    try:
        await send_log_api(
            f"💳 **PLAN CHECKED**\n\n"
            f"👤 [{first_name}](tg://user?id={user_id})\n"
            f"🆔 `{user_id}`\n"
            f"💎 **Plan:** `{plan['name']}`\n"
            f"📊 **Tasks:** `{tasks_done}`"
        )
    except: pass

# ═══════════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════════
@Client.on_callback_query(filters.regex(r"^plan_info:(.+)$"))
async def plan_info_callback(client, callback):
    plan_key = callback.data.split(":")[1]
    if plan_key not in PLANS:
        await callback.answer("❌ Invalid plan!", show_alert=True)
        return
    text = build_plan_detail(plan_key)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Buy This Plan", url="https://t.me/Univora_Support")],
        [InlineKeyboardButton("⬅️ Back to My Plans", callback_data="show_plans_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except: pass
    await callback.answer()

@Client.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_callback(client, callback):
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name or "User"

    # Double-check trial status
    if await has_used_trial(user_id):
        await callback.answer("❌ You've already used your free trial!", show_alert=True)
        return

    # Activate trial
    expiry = time.time() + 86400  # 24 hours
    await set_subscription(user_id, "trial", expiry)
    await mark_trial_used(user_id)

    exp_str = datetime.datetime.fromtimestamp(expiry).strftime("%d %b %Y • %I:%M %p")

    text = (
        "╔══════════════════════╗\n"
        "║  🎁  TRIAL ACTIVATED!  ║\n"
        "╚══════════════════════╝\n\n"
        f"🎉 Congratulations, **{first_name}**!\n\n"
        f"Your **Free Trial** is now active!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ **Expires:** `{exp_str}`\n"
        f"🔢 **Tasks:** `1`\n"
        f"🔗 **Fast Copy:** `20,000` files\n"
        f"📦 **DL+Upload:** `5,000` files\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Use /batch to start extracting!\n\n"
        f"💡 _Want more? Check /showplan for premium plans._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Extracting!", callback_data="start_batch")],
        [InlineKeyboardButton("💎 View Premium Plans", callback_data="show_plans_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except:
        await callback.answer("✅ Trial Activated!", show_alert=True)

    # Log
    try:
        await send_log_api(
            f"🎁 **TRIAL ACTIVATED**\n\n"
            f"👤 [{first_name}](tg://user?id={user_id})\n"
            f"🆔 `{user_id}`\n"
            f"⏳ **Expires:** `{exp_str}`"
        )
    except: pass

@Client.on_callback_query(filters.regex("^trial_used_notice$"))
async def trial_used_notice(client, callback):
    await callback.answer(
        "❌ You've already used your Free Trial!\n\n"
        "Tap any paid plan below to upgrade 👇",
        show_alert=True
    )

@Client.on_callback_query(filters.regex("^show_plans_back$"))
async def show_plans_back_callback(client, callback):
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name or "User"

    plan_key, plan, tasks_done, expiry = await get_resolved_plan(user_id)
    text = build_status_card(first_name, user_id, plan_key, plan, tasks_done, expiry)
    kb = await build_plan_keyboard(user_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except: pass
    await callback.answer()

# ═══════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════
@Client.on_message(filters.command("addpremium") & filters.user(int(OWNER_ID)))
async def add_premium(client, message):
    try:
        args = message.command
        plan_ids_str = "\n".join([
            f"  `{k}` — {v['name']} ({v['price']})"
            for k, v in PLANS.items() if k not in ("free", "trial")
        ])
        if len(args) < 3:
            await message.reply_text(
                f"**Usage:** `/addpremium <user_id> <plan_id>`\n\n"
                f"**Available Plans:**\n{plan_ids_str}"
            )
            return

        target_id = int(args[1])
        plan_id = LEGACY_PLAN_MAP.get(args[2].lower(), args[2].lower())

        if plan_id not in PLANS or plan_id in ("free", "trial"):
            await message.reply_text("❌ Invalid Plan ID. Use one of: `daily_39`, `monthly_259`, `ultra_389`, `lifetime_2999`")
            return

        plan = PLANS[plan_id]
        dur = plan["duration"]
        expiry = (time.time() + dur) if dur > 0 else 0
        await set_subscription(target_id, plan_id, expiry)

        exp_str = datetime.datetime.fromtimestamp(expiry).strftime("%d %b %Y %H:%M") if expiry > 0 else "Lifetime"
        await message.reply_text(
            f"✅ **Premium Activated!**\n\n"
            f"👤 User: `{target_id}`\n"
            f"{plan['emoji']} Plan: **{plan['name']}**\n"
            f"⏳ Expires: `{exp_str}`"
        )
        try:
            await send_log_api(
                f"🎁 **PREMIUM GRANTED**\n\n"
                f"👤 **User:** `{target_id}`\n"
                f"💎 **Plan:** `{plan['name']}`\n"
                f"⏳ **Expires:** `{exp_str}`\n"
                f"👮 **By:** `{message.from_user.id}`"
            )
        except: pass
        try:
            await client.send_message(
                target_id,
                f"🎉 **Premium Activated!**\n\n"
                f"You've been upgraded to **{plan['name']}**!\n"
                f"⏳ Valid until: `{exp_str}`\n\n"
                f"Use /showplan to see your full limits.\n"
                f"⚡ _Powered by Univora_"
            )
        except: pass
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@Client.on_message(filters.command("removepremium") & filters.user(int(OWNER_ID)))
async def remove_premium(client, message):
    try:
        args = message.command
        if len(args) < 2:
            await message.reply_text("Usage: `/removepremium <user_id>`")
            return
        target_id = int(args[1])
        await set_subscription(target_id, "free", 0)
        await message.reply_text(f"✅ **Premium Removed.** User `{target_id}` is back on Free Plan.")
        try:
            await send_log_api(
                f"🔻 **PREMIUM REVOKED**\n\n"
                f"👤 **User:** `{target_id}`\n"
                f"👮 **By:** `{message.from_user.id}`"
            )
        except: pass
    except Exception as e:
        await message.reply_text(f"Error: {e}")

@Client.on_message(filters.command("givetrial") & filters.user(int(OWNER_ID)))
async def give_trial(client, message):
    """Admin: Reset and grant trial to a specific user."""
    try:
        args = message.command
        if len(args) < 2:
            await message.reply_text("Usage: `/givetrial <user_id>`")
            return
        target_id = int(args[1])
        database = await get_db()
        await database.trial_used.delete_one({"_id": target_id})
        expiry = time.time() + 86400
        await set_subscription(target_id, "trial", expiry)
        await mark_trial_used(target_id)
        await message.reply_text(f"✅ Trial reset & granted to `{target_id}` (24h).")
    except Exception as e:
        await message.reply_text(f"Error: {e}")
