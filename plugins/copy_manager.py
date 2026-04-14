from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from database import get_session, get_settings, is_protected_channel
from config import API_ID, API_HASH
from plugins.subscription import check_user_access, record_task_use, check_force_sub
import asyncio
import logging
import time
import os

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
    
    # Session will be checked later if the URL is private.
    
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
        if "t.me/" not in text:
            await message.reply_text("❌ Invalid Telegram URL. Try again.")
            return True
        
        # Parse channel ID from link
        try:
            raw_link = text.rstrip("/").split("?")[0]
            parts = raw_link.split("/")
            
            if "c" in parts:
                if not await get_session(user_id):
                    await message.reply_text("⛔ You must be logged in to extract from private links!\nUse /login first.")
                    return True
                channel_id_str = parts[-2]
                source_id = int(f"-100{channel_id_str}")
            else:
                source_id = parts[-2]
            
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
        
        # Parse Link First to Determine Worker Type
        try:
            # Clean link: specifically strip off query parameters (like ?single)
            raw_link = link.rstrip("/").split("?")[0]
            parts = raw_link.split("/")
            start_msg_id = int(parts[-1])
            
            if "c" in parts:
                channel_id_str = parts[-2]
                source_id = int(f"-100{channel_id_str}")
                is_public = False
            else:
                source_id = parts[-2] # Public channel username
                is_public = True

            # Record Usage (Charge User)
            await record_task_use(user_id)
            
            if is_public:
                userbot = bot
            else:
                if not session:
                    await status_msg.edit_text("🚫 **Login Error**\n\nYou need to login to extract from private channels.")
                    if user_id in active_jobs: del active_jobs[user_id]
                    return
                    
                userbot = Client(f"worker_{user_id}", api_id=API_ID, api_hash=API_HASH, session_string=session, in_memory=True)
                try:
                    await userbot.start()
                except Exception as e:
                    await status_msg.edit_text(f"🚫 **Login Error**\n\nCould not connect to user account.\nReason: `{e}`")
                    if user_id in active_jobs: del active_jobs[user_id]
                    return
            
            # 1. Verify Access & Get Chat Info
            real_chat_id = source_id
            chat_title = "Channel"
            try:
                chat = await userbot.get_chat(source_id)
                real_chat_id = chat.id
                chat_title = chat.title or "Unknown Channel"
            except Exception as e:
                 # Fallback: Search Dialogs (useful if Telegram restricts get_chat on some peers)
                 logger.error(f"get_chat failed for {source_id}: {e}")
                 found_dialog = False
                 async for d in userbot.get_dialogs():
                     if active_jobs[user_id]["cancel"]: break
                     
                     match_id = d.chat.id == source_id
                     match_username = isinstance(source_id, str) and d.chat.username and d.chat.username.lower() == source_id.lower()
                     
                     if match_id or match_username:
                         real_chat_id = d.chat.id
                         chat_title = d.chat.title or "Channel"
                         found_dialog = True
                         break
                 if not found_dialog:
                     await status_msg.edit_text("❌ **Source Not Found**\n\nThe bot cannot access this channel. Ensure the link is correct and you have joined the channel if it is private.")
                     if 'userbot' in locals() and userbot != bot:
                         try: await userbot.stop()
                         except: pass
                     if user_id in active_jobs: del active_jobs[user_id]
                     return
            
            # 2. Get Real Last Message ID (Crucial for Stopping)
            real_last_msg_id = 0
            if is_public:
                # Bots cannot use get_chat_history. Synthesize upper bound ceiling dynamically.
                real_last_msg_id = start_msg_id + (int(limit) if limit != float('inf') else 100000)
            else:
                async for last_msg in userbot.get_chat_history(real_chat_id, limit=1):
                    real_last_msg_id = last_msg.id
                    break
            
            if real_last_msg_id == 0:
                await status_msg.edit_text("⚠️ **Channel Empty**\n\nNo messages found in the source channel.")
                if 'userbot' in locals() and userbot != bot:
                    try: await userbot.stop()
                    except: pass
                if user_id in active_jobs: del active_jobs[user_id]
                return
            
            if start_msg_id > real_last_msg_id:
                await status_msg.edit_text(
                    f"⚠️ **Range Error**\n\n"
                    f"Start ID (`{start_msg_id}`) is higher than Last ID (`{real_last_msg_id}`).\n"
                    f"Maybe the bot joined a different channel or history is restricted?"
                )
                if 'userbot' in locals() and userbot != bot:
                    try: await userbot.stop()
                    except: pass
                if user_id in active_jobs: del active_jobs[user_id]
                return
                     
        except Exception as e:
            await status_msg.edit_text(f"❌ **Link Error**\n\nFailed to parse link: `{e}`")
            worker_client = locals().get('userbot')
            if worker_client and worker_client != bot:
                try: await worker_client.stop()
                except: pass
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
        logger.info(f"Workload Debug: Target={target_stop_id}, Start={start_msg_id}, Limit={limit}, Result={total_workload}")
        if limit and limit < total_workload: total_workload = limit
        burst_count = 0
        
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
        consecutive_empty_batches = 0
        last_update_time = time.time()
        
        while True:
            try: userbot.sleep_threshold = 5
            except: pass
            
            # 1. Global Checks
            if active_jobs[user_id]["cancel"]: break
            # Stop if we passed the user's limit
            if limit and copied >= limit: break
            # Stop if we passed the requested range OR the channel end
            if current_id > target_stop_id or current_id > real_last_msg_id: break
            
            # Batch Fetch
            batch_size = 50
            end_id = current_id + batch_size
            ids_to_fetch = list(range(current_id, end_id))
            
            try:
                msgs = await userbot.get_messages(real_chat_id, ids_to_fetch)
                if not isinstance(msgs, list): msgs = [msgs]
                
                # Check empty batch (skipped IDs)
                valid_msgs = [m for m in msgs if m and not m.empty]
                if not valid_msgs:
                     consecutive_empty_batches += 1
                     if consecutive_empty_batches > 3: # End of channel reached
                         break
                     # Just move forward, maybe just large gaps
                else:
                     consecutive_empty_batches = 0

                for msg in msgs:
                    # Inner Checks
                    if active_jobs[user_id]["cancel"]: break
                    if limit and copied >= limit: break
                    if msg and msg.id > target_stop_id: break # Strict verify
                    
                    # Filter & Process
                    if not msg or msg.empty or msg.service: continue
                    
                    # Check Type
                    ok = False
                    if "All Content" in active_f: ok = True 
                    elif filters_set.get("all"): ok = True
                    else:
                        if msg.media:
                             mtype = msg.media
                             if mtype == enums.MessageMediaType.PHOTO and filters_set.get("photo"): ok = True
                             elif mtype == enums.MessageMediaType.VIDEO and filters_set.get("video"): ok = True
                             elif mtype == enums.MessageMediaType.DOCUMENT:
                                 if filters_set.get("document"): ok = True
                                 # Smart Filter: Allow video/image documents
                                 elif filters_set.get("video") and msg.document.mime_type and str(msg.document.mime_type).startswith("video/"): ok = True
                                 elif filters_set.get("photo") and msg.document.mime_type and str(msg.document.mime_type).startswith("image/"): ok = True
                             
                             # Enhanced "Media Only" Logic
                             elif filters_set.get("media"): 
                                 # Allow minimal media (Audio/Voice/Animation) if specific toggles OFF
                                 # We remove PHOTO/VIDEO/DOCUMENT here so "Media" doesn't override their specific OFF toggles
                                 if mtype in [enums.MessageMediaType.AUDIO, enums.MessageMediaType.VOICE, enums.MessageMediaType.ANIMATION]:
                                     ok = True
                        else:
                            if msg.text and filters_set.get("text"): ok = True
                    
                    if not ok: continue
                    
                    # Caption Logic
                    original_cap = msg.caption or (msg.text if not msg.media else "") or ""
                    
                    # Apply Removals/Replacements
                    if original_cap:
                        for rem in caption_rules.get("removals", []): 
                            original_cap = original_cap.replace(rem, "")
                        for old, new in caption_rules.get("replacements", {}).items(): 
                            original_cap = original_cap.replace(old, new)
                        original_cap = original_cap.strip()
                    
                    # Construct Final
                    p = caption_rules.get("prefix", "")
                    s = caption_rules.get("suffix", "")
                    
                    parts = []
                    if p: parts.append(p)
                    if original_cap: parts.append(original_cap)
                    if s: parts.append(s)
                    
                    final_caption = "\n".join(parts) if parts else None

                    # Copy Phase
                    # Copy Phase
                    # Copy Phase
                    uploaded_restricted_msg = None
                    
                    def get_progress_func(action_name):
                        last_update = [time.time()]
                        async def progress(current, total):
                            now = time.time()
                            if (now - last_update[0]) > 4:
                                percent = (current / total) * 100 if total else 0
                                curr_mb = current / (1024 * 1024)
                                tot_mb = total / (1024 * 1024) if total else 0
                                try:
                                    await status_msg.edit_text(
                                        f"⚙️ **Manual Extraction** ⚙️\n\n"
                                        f"🚀 **Action:** `{action_name}`\n"
                                        f"📊 **Progress:** `{percent:.1f}%`\n"
                                        f"📦 **Size:** `{curr_mb:.1f} MB` / `{tot_mb:.1f} MB`\n\n"
                                        f"_(Fast multi-target sync enabled)_"
                                    )
                                    last_update[0] = now
                                except: pass
                        return progress

                    for i, dest in enumerate(dest_channels):
                        if active_jobs[user_id]["cancel"]: break
                        try: d_id = int(dest)
                        except: d_id = dest
                        
                        # Retry Mechanism
                        success = False
                        for attempt in range(3):
                            if active_jobs[user_id]["cancel"]: break
                            try:
                                # Optimization: If we already manually uploaded it to one destination,
                                # we can just forward IT to the other destinations instantly!
                                if uploaded_restricted_msg is not None:
                                    await userbot.copy_message(
                                        chat_id=d_id, 
                                        from_chat_id=uploaded_restricted_msg.chat.id, 
                                        message_id=uploaded_restricted_msg.id, 
                                        caption=final_caption
                                    )
                                    success = True
                                    break

                                # Try fast copy first
                                try:
                                    await userbot.copy_message(
                                        chat_id=d_id, 
                                        from_chat_id=real_chat_id, 
                                        message_id=msg.id, 
                                        caption=final_caption
                                    )
                                except Exception as e:
                                    # Check for Restricted Content Error
                                    err_str = str(e)
                                    if "CHAT_FORWARDS_RESTRICTED" in err_str or "restricted" in err_str.lower() or "can't copy" in err_str.lower():
                                        # Notify user IMMEDIATELY
                                        try:
                                            await status_msg.edit_text(
                                                f"🔒 **Restricted Content Detected**\n\n"
                                                f"Channel blocks forwarding.\n"
                                                f"Switching to **Download/Upload Mode**..."
                                            )
                                        except: pass
                                        
                                        # Fallback: Manual Extraction (Download & Upload)
                                        sent_msg = None
                                        if msg.text:
                                            sent_msg = await userbot.send_message(d_id, final_caption or msg.text)
                                        elif msg.media:
                                            f_path = await userbot.download_media(msg, progress=get_progress_func("Downloading from Source"))
                                            
                                            try:
                                                # Upload based on type
                                                if msg.photo:
                                                    sent_msg = await userbot.send_photo(d_id, f_path, caption=final_caption, progress=get_progress_func("Uploading to Target"))
                                                elif msg.video:
                                                    thumb_path = None
                                                    if getattr(msg.video, "thumbs", None):
                                                        thumb_path = await userbot.download_media(msg.video.thumbs[0].file_id)
                                                    
                                                    try:
                                                        sent_msg = await userbot.send_video(
                                                            d_id, 
                                                            f_path, 
                                                            caption=final_caption, 
                                                            duration=msg.video.duration, 
                                                            width=msg.video.width, 
                                                            height=msg.video.height, 
                                                            thumb=thumb_path,
                                                            progress=get_progress_func("Uploading to Target")
                                                        )
                                                    finally:
                                                        if thumb_path and os.path.exists(thumb_path):
                                                            os.remove(thumb_path)
                                                elif msg.document:
                                                    sent_msg = await userbot.send_document(d_id, f_path, caption=final_caption, force_document=True, progress=get_progress_func("Uploading to Target"))
                                                elif msg.audio:
                                                    sent_msg = await userbot.send_audio(d_id, f_path, caption=final_caption, duration=msg.audio.duration, performer=msg.audio.performer, title=msg.audio.title, progress=get_progress_func("Uploading to Target"))
                                                elif msg.voice:
                                                    sent_msg = await userbot.send_voice(d_id, f_path, caption=final_caption, duration=msg.voice.duration, progress=get_progress_func("Uploading to Target"))
                                                elif msg.animation:
                                                    sent_msg = await userbot.send_animation(d_id, f_path, caption=final_caption, progress=get_progress_func("Uploading to Target"))
                                                elif msg.sticker:
                                                    sent_msg = await userbot.send_sticker(d_id, f_path, progress=get_progress_func("Uploading to Target"))
                                                else:
                                                    # Generic doc fallback
                                                    sent_msg = await userbot.send_document(d_id, f_path, caption=final_caption, progress=get_progress_func("Uploading to Target"))
                                            finally:
                                                # Cleanup
                                                if f_path and os.path.exists(f_path):
                                                    os.remove(f_path)
                                        
                                        # Save the manually uploaded message to instantly forward it to other targets!
                                        if sent_msg:
                                            uploaded_restricted_msg = sent_msg
                                    else:
                                        raise e # Re-raise if not restricted error

                                burst_count += 1
                                if burst_count >= 20: 
                                    burst_count = 0
                                    await asyncio.sleep(10) # Cooling Period
                                else:
                                    await asyncio.sleep(0.1) # Fast Burst

                                success = True
                                break # Done for this destination
                                
                            except FloodWait as e:
                                logger.warning(f"FloodWait: Sleeping {e.value}s")
                                await asyncio.sleep(e.value + 2)
                            except Exception as e:
                                logger.error(f"Copy Fail (Attempt {attempt+1}): {e}")
                                await asyncio.sleep(2)
                        
                        if not success:
                            logger.error(f"Failed to copy message {msg.id} to {dest} after retries.")
                    
                    copied += 1
                    
                    # Live Dashboard Update
                    now = time.time()
                    if now - last_update_time > 8.0:
                        last_update_time = now
                        
                        # Calculate Percentage
                        percent = 0
                        if total_workload > 0:
                            percent = int((copied / total_workload) * 100)
                        
                        if percent >= 100: percent = 99
                        
                        bar = get_progress_bar(copied, total_workload, length=12)
                        
                        try:
                            await status_msg.edit_text(
                                f"⚡ **EXTRACT X PROCESSOR** ⚡\n\n"
                                f"📥 **Processing:** `{copied}` / `{total_workload}`\n"
                                f"`{bar}` **{percent}%**\n\n"
                                f"🟢 **Status:** `Active & Copying...`\n"
                                f" **Source:** `{chat_title}`\n"
                                f"📁 **Current Filter:** {filter_str}\n\n"
                                f"_* Press /cancel to stop immediately._"
                            )
                        except FloodWait as e:
                            logger.warning(f"UI FloodWait: {e.value}s - Skipping update")
                            # Don't sleep, just skip UI update to keep extracting
                            last_update_time = now + e.value # dampen updates for a while
                        except Exception as e:
                            logger.error(f"UI Update Fail: {e}")
                    
                    # await asyncio.sleep(1.0) # Removed for Burst Speed
                
            except FloodWait as e:
                logger.warning(f"Batch FloodWait: {e.value}s")
                try: await status_msg.edit_text(f"⏳ **Rate Limited**\n\nTelegram says wait `{e.value}s`.\nI'll wait and retry this batch.")
                except: pass
                await asyncio.sleep(e.value + 2)
                fail_count = 0
                continue 
                
            except Exception as e:
                logger.error(f"Batch Error: {e}")
                # Don't break on simple fetch errors, just skip batch
                fail_count += 1
                if fail_count > 5: break
            
            if active_jobs[user_id]["cancel"]: break
            current_id += batch_size
            await asyncio.sleep(2.0) # Safety between GetMessages
        
        worker_client = locals().get('userbot')
        if worker_client and worker_client != bot:
            try: await worker_client.stop()
            except: pass
        
        # Final Report Card
        final_text = ""
        if active_jobs[user_id]["cancel"]:
             final_text = (
                "🛑 **PROCESS CANCELLED**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 **User:** `{user_id}`\n"
                f"📉 **Progress:** Stopped by user\n"
                f"✅ **Succesfully Copied:** `{copied}` Items\n"
                "━━━━━━━━━━━━━━━━━━"
             )
        else:
             final_text = (
                "✅ **MISSION ACCOMPLISHED**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📂 **Source:** `{chat_title}`\n"
                f"📊 **Total Extracted:** `{copied}` Items\n"
                f"🎯 **Target Reached:** `100%`\n"
                f"⏱ **Status:** `Completed Successfully`\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🤖 *Thank you for using ExtractX*"
             )
        
        try:
            await status_msg.edit_text(final_text)
        except Exception:
            # Fallback if edit fails (e.g. FloodWait)
            await message.reply_text(final_text)
             
    except Exception as e:
        err_msg = f"❌ **Critical System Error**\n\n`{e}`"
        try:
            await status_msg.edit_text(err_msg)
        except Exception:
            await message.reply_text(err_msg)
    
    # Cleanup
    if user_id in active_jobs:
        del active_jobs[user_id]
