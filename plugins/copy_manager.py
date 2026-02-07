from pyrogram import Client, filters, enums
from pyrogram.types import Message
from database import get_session, get_settings, is_protected_channel
from config import API_ID, API_HASH
from plugins.subscription import check_user_access, record_task_use, check_force_sub
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# State for batch command
# batch_states[user_id] = {step: "LINK" or "COUNT", link: "..."}
batch_states = {}
# active_jobs[user_id] = {"cancel": False, "status_msg": ...}
active_jobs = {}

@Client.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client, message):
    user_id = message.from_user.id
    if user_id in active_jobs:
        job = active_jobs[user_id]
        job["cancel"] = True
        
        # Immediate Feedback
        if "status_msg" in job:
            try:
                await job["status_msg"].edit_text("🛑 **Cancelling... Stopping Process.**")
            except:
                pass
                
        await message.reply_text("✅ Process Cancelled.")
    else:
        await message.reply_text("⚠️ No active batch process found.")

@Client.on_message(filters.command("batch") & filters.private)
async def batch_start(client, message):
    if not await check_force_sub(client, message):
        return

    user_id = message.from_user.id
    
    # Check Login
    if not await get_session(user_id):
        await message.reply_text("⛔ You are not logged in!\nUse /login first.")
        return
    
    # Check already running
    if user_id in active_jobs:
        await message.reply_text("⚠️ A task is already running.\nUse /cancel to stop it first.")
        return
        
    # Check Subscription Access
    allowed, reason, file_limit, remaining = await check_user_access(user_id)
    if not allowed:
        await message.reply_text(reason)
        return

    # Check Destination
    settings = await get_settings(user_id)
    if not settings or not settings.get("dest_channels"):
        await message.reply_text("⛔ No destination channels set!\nUse /settings > Channel Manager > Add Channel.")
        return

    batch_states[user_id] = {"step": "LINK"}
    await message.reply_text(
        "🚀 **Batch Extraction Started**\n\n"
        "Send the **Link** of the starting message from the private channel.\n"
        "Example: `https://t.me/c/123456789/100`"
    )

async def handle_batch_input(client, message):
    user_id = message.from_user.id
    if user_id not in batch_states:
        return False
        
    # Ignore commands
    if message.text and message.text.startswith("/"):
        return False
        
    state = batch_states[user_id]
    step = state["step"]
    text = message.text.strip()
    
    if step == "LINK":
        if "t.me/c/" not in text:
            await message.reply_text("❌ Invalid Private Channel Link. Try again.")
            return True
        
        # Parse channel ID from link
        try:
            parts = text.split("/")
            channel_id_str = parts[-2]
            source_id = int(f"-100{channel_id_str}")
            
            # Check if protected
            if await is_protected_channel(source_id):
                del batch_states[user_id]
                await message.reply_text(
                    "🔮 **Protected Channel Detected!** 🪄\n\n"
                    "Whoa there! This channel is under a magical protection spell! ✨\n\n"
                    "**मेरा जादू मुझ पर ही चलेगा!**\n"
                    "_(My magic works for me only!)_\n\n"
                    "You cannot extract from this protected channel. 🛡️\n\n"
                    "Try another channel or contact the admin if you think this is an error."
                )
                return True
        except Exception as e:
            logger.error(f"Error parsing channel link: {e}")
        
        state["link"] = text
        state["step"] = "COUNT"
        
        # Check Limits Again to display hints
        allowed, reason, file_limit, remaining = await check_user_access(user_id)
        limit_txt = f"{file_limit}" if file_limit != float('inf') else "Unlimited"
        
        await message.reply_text(
            "🔢 **How many messages to copy?**\n\n"
            f"⚠️ **Your Plan Limit:** `{limit_txt}` Files/Task\n\n"
            "• Type a number (e.g., `10`, `100`)\n"
            "• Type `all` to copy everything (upto limit)."
        )
        return True
        
    elif step == "COUNT":
        count_val = text.lower()
        limit = None
        
        # Validate Limit vs Plan
        allowed, reason, file_limit, remaining = await check_user_access(user_id)
        if not allowed:
            await message.reply_text(reason)
            del batch_states[user_id]
            return True
            
        if count_val != "all":
            if not count_val.isdigit():
                 await message.reply_text("❌ Invalid number. Enter a number or 'all'.")
                 return True
            limit = int(count_val)
            
            if limit > file_limit:
                 await message.reply_text(
                     f"⚠️ **Upgrade Required**\n\n"
                     f"Your current plan allows max `{file_limit}` files per task.\n"
                     f"You requested `{limit}`.\n\n"
                     "Type a smaller number or /showplan to upgrade."
                 )
                 return True # Retry
        else:
             # If all, we set limit to file_limit if it's finite
             if file_limit != float('inf'):
                 limit = int(file_limit)
                 await message.reply_text(f"⚠️ 'All' selected. Capping at `{limit}` due to plan limit.")
            
        # Start Process
        link = state["link"]
        del batch_states[user_id] # Clear State
        
        await start_copy_job(client, message, user_id, link, limit)
        return True
    
    return False

def get_progress_bar(current, total, length=10):
    if total == 0: return "░" * length
    percent = current / total
    filled = int(length * percent)
    filled = max(0, min(length, filled))
    return "▓" * filled + "░" * (length - filled)

async def start_copy_job(bot, message, user_id, link, limit):
    # initializing UI
    status_msg = await message.reply_text(
        "🔄 **System Initializing...**\n"
        "Please wait while I connect to the source."
    )
    active_jobs[user_id] = {"cancel": False, "status_msg": status_msg}
    
    try:
        session = await get_session(user_id)
        settings = await get_settings(user_id)
        dest_channels = settings["dest_channels"]
        filters_set = settings["filters"]
        caption_rules = settings.get("caption_rules", {})
        
        # Start Userbot
        userbot = Client(f"worker_{user_id}", api_id=API_ID, api_hash=API_HASH, session_string=session, in_memory=True)
        try:
            await userbot.start()
            # Record Usage (Charge User)
            await record_task_use(user_id)
        except Exception as e:
            await status_msg.edit_text(f"🚫 **Login Error**\n\nCould not connect to user account.\nReason: `{e}`")
            if user_id in active_jobs: del active_jobs[user_id]
            return

        # Parse Link
        try:
            parts = link.split("/")
            channel_id_str = parts[-2]
            start_msg_id = int(parts[-1])
            source_id = int(f"-100{channel_id_str}")
            
            # 1. Verify Access & Get Chat Info
            real_chat_id = source_id
            chat_title = "Private Channel"
            try:
                chat = await userbot.get_chat(source_id)
                real_chat_id = chat.id
                chat_title = chat.title or "Unknown Channel"
            except:
                 # Fallback: Search Dialogs
                 found_dialog = False
                 async for d in userbot.get_dialogs():
                     if active_jobs[user_id]["cancel"]: break
                     if d.chat.id == source_id:
                         real_chat_id = d.chat.id
                         chat_title = d.chat.title or "Private Channel"
                         found_dialog = True
                         break
                 if not found_dialog:
                     await status_msg.edit_text("❌ **Source Not Found**\n\nThe bot cannot access this channel. Ensure you have joined it.")
                     await userbot.stop()
                     if user_id in active_jobs: del active_jobs[user_id]
                     return
            
            # 2. Get Real Last Message ID (Crucial for Stopping)
            real_last_msg_id = 0
            async for last_msg in userbot.get_chat_history(real_chat_id, limit=1):
                real_last_msg_id = last_msg.id
                break
            
            if real_last_msg_id == 0:
                await status_msg.edit_text("⚠️ **Channel Empty**\n\nNo messages found in the source channel.")
                await userbot.stop()
                if user_id in active_jobs: del active_jobs[user_id]
                return
                     
        except Exception as e:
            await status_msg.edit_text(f"❌ **Link Error**\n\nFailed to parse link: `{e}`")
            await userbot.stop()
            if user_id in active_jobs: del active_jobs[user_id]
            return

        # Calc Targets
        # If user said 10, but start=100 and last=105, we can only copy 5 (~6).
        # We need to respect the smaller constraint.
        
        target_stop_id = 0
        
        if limit is None: # ALL
            target_stop_id = real_last_msg_id
        else:
            # If start is 100, limit is 10, target is 110. But if real last is 105, we stop at 105.
            calc_stop = start_msg_id + limit
            target_stop_id = min(calc_stop, real_last_msg_id)
            
        total_workload = max(1, target_stop_id - start_msg_id)
        if limit and limit < total_workload: total_workload = limit
        
        # Prepare Active Filters Text
        active_f = []
        if filters_set.get("all"): active_f.append("All Content")
        else:
             if filters_set.get("photo"): active_f.append("📸 Photos")
             if filters_set.get("video"): active_f.append("📹 Videos")
             if filters_set.get("document"): active_f.append("📂 Files")
             if filters_set.get("text"): active_f.append("📝 Text")
        filter_str = " | ".join(active_f) if active_f else "None"

        # Initial Dashboard
        await status_msg.edit_text(
            f"⚡ **EXTRACT X PROCESSOR** ⚡\n\n"
            f"📡 **Source:** `{chat_title}`\n"
            f"🎯 **Target:** `{len(dest_channels)} Destination(s)`\n"
            f"🛠 **Filters:** {filter_str}\n\n"
            f"📊 **Workload:** ~`{total_workload}` Messages\n"
            f"🚀 **Status:** `Starting Engine...`"
        )
        
        copied = 0
        current_id = start_msg_id
        fail_count = 0
        last_update_time = time.time()
        
        while True:
            # 1. Global Checks
            if active_jobs[user_id]["cancel"]: break
            # Stop if we passed the user's limit
            if limit and copied >= limit: break
            # Stop if we passed the requested range OR the channel end
            if current_id > target_stop_id or current_id > real_last_msg_id: break
            
            # Batch Fetch
            batch_size = 20
            end_id = current_id + batch_size
            ids_to_fetch = list(range(current_id, end_id))
            
            try:
                msgs = await userbot.get_messages(real_chat_id, ids_to_fetch)
                if not isinstance(msgs, list): msgs = [msgs]
                
                # Check empty batch (skipped IDs)
                valid_msgs = [m for m in msgs if m and not m.empty]
                if not valid_msgs and batch_size > 0:
                     # Just move forward, don't fail immediately, maybe just gaps
                     pass 

                for msg in msgs:
                    # Inner Checks
                    if active_jobs[user_id]["cancel"]: break
                    if limit and copied >= limit: break
                    if msg and msg.id > target_stop_id: break # Strict verify
                    
                    # Filter & Process
                    if not msg or msg.empty or msg.service: continue
                    
                    # Check Type
                    ok = False
                    if "All Content" in active_f: ok = True # Derived from previous list check logic
                    elif filters_set.get("all"): ok = True
                    else:
                        if msg.media:
                             mtype = msg.media
                             if mtype == enums.MessageMediaType.PHOTO and filters_set.get("photo"): ok = True
                             elif mtype == enums.MessageMediaType.VIDEO and filters_set.get("video"): ok = True
                             elif mtype == enums.MessageMediaType.DOCUMENT and filters_set.get("document"): ok = True
                             elif filters_set.get("media"): ok = True
                        else:
                            if msg.text and filters_set.get("text"): ok = True
                    
                    if not ok: continue
                    
                    # Caption Logic
                    final_caption = msg.caption or (msg.text if not msg.media else "") or ""
                    if final_caption:
                        for rem in caption_rules.get("removals", []): final_caption = final_caption.replace(rem, "")
                        for old, new in caption_rules.get("replacements", {}).items(): final_caption = final_caption.replace(old, new)
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

                    # Copy Phase
                    for dest in dest_channels:
                        if active_jobs[user_id]["cancel"]: break
                        try:
                            try: d_id = int(dest)
                            except: d_id = dest
                            
                            await userbot.copy_message(chat_id=d_id, from_chat_id=real_chat_id, message_id=msg.id, caption=final_caption)
                            await asyncio.sleep(0.2)
                        except Exception as e:
                            logger.error(f"Copy Fail: {e}")
                    
                    copied += 1
                    
                    # Live Dashboard Update
                    now = time.time()
                    if now - last_update_time > 2.5:
                        last_update_time = now
                        
                        # Calculate Percentage
                        # Logic: (Copied / Workload) * 100
                        percent = 0
                        if total_workload > 0:
                            percent = int((copied / total_workload) * 100)
                        
                        # Don't show 100% until actually done
                        if percent >= 100: percent = 99
                        
                        bar = get_progress_bar(copied, total_workload, length=12)
                        
                        await status_msg.edit_text(
                            f"⚡ **EXTRACT X PROCESSOR** ⚡\n\n"
                            f"📥 **Processing:** `{copied}` / `{total_workload}`\n"
                            f"`{bar}` **{percent}%**\n\n"
                            f"🟢 **Status:** `Active & Copying...`\n"
                            f"� **Source:** `{chat_title}`\n"
                            f"📁 **Current Filter:** {filter_str}\n\n"
                            f"_* Press /cancel to stop immediately._"
                        )
                    
                    await asyncio.sleep(1.0) # Flood Protection
                
            except Exception as e:
                logger.error(f"Batch Error: {e}")
                # Don't break on simple fetch errors, just skip batch
                fail_count += 1
                if fail_count > 5: break
            
            if active_jobs[user_id]["cancel"]: break
            current_id += batch_size
        
        await userbot.stop()
        
        # Final Report Card
        if active_jobs[user_id]["cancel"]:
             await status_msg.edit_text(
                "🛑 **PROCESS CANCELLED**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 **User:** `{user_id}`\n"
                f"📉 **Progress:** Stopped by user\n"
                f"✅ **Succesfully Copied:** `{copied}` Items\n"
                "━━━━━━━━━━━━━━━━━━"
             )
        else:
             await status_msg.edit_text(
                "✅ **MISSION ACCOMPLISHED**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📂 **Source:** `{chat_title}`\n"
                f"📊 **Total Extracted:** `{copied}` Items\n"
                f"🎯 **Target Reached:** `100%`\n"
                f"⏱ **Status:** `Completed Successfully`\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🤖 *Thank you for using ExtractX*"
             )
             
    except Exception as e:
        await status_msg.edit_text(f"❌ **Critical System Error**\n\n`{e}`")
    
    # Cleanup
    if user_id in active_jobs:
        del active_jobs[user_id]
