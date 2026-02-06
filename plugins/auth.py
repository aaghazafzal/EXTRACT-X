from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from database import delete_session, save_session, get_session
from config import API_ID, API_HASH
import asyncio

# Temp storage for auth Steps
auth_states = {}

from plugins.subscription import check_force_sub

@Client.on_message(filters.command("login") & filters.private)
async def login_start(client, message):
    if not await check_force_sub(client, message):
        return
    user_id = message.from_user.id
    
    # Check if already logged in
    if await get_session(user_id):
        await message.reply_text("✅ You are already logged in!\nUse /logout to remove your session.")
        return

    auth_states[user_id] = {"step": "PHONE"}
    await message.reply_text(
        "🔐 **Login process started.**\n\n"
        "Please enter your **Phone Number** in international format.\n"
        "Example: `+919876543210`"
    )

@Client.on_message(filters.command("logout") & filters.private)
async def logout_handler(client, message):
    await delete_session(message.from_user.id)
    await message.reply_text("👋 You have been logged out and your data cleared.")

async def handle_auth_input(client, message):
    user_id = message.from_user.id
    if user_id not in auth_states:
        return False
        
    # Ignore commands, let other handlers process them
    if message.text and message.text.startswith("/"):
        return False
    
    state = auth_states[user_id]
    step = state.get("step")
    text = message.text.strip()

    try:
        if step == "PHONE":
            # Sanitize Phone Number
            clean_phone = text.replace(" ", "").replace("-", "").strip()
            
            msg = await message.reply_text(f"⏳ Connecting with `{clean_phone}`...")
            
            # Start a temporary client for this user
            # We use distinct memory storage for each auth attempt
            temp_client = Client(
                f"auth_{user_id}", 
                api_id=API_ID, 
                api_hash=API_HASH,
                in_memory=True
            )
            await temp_client.connect()
            
            try:
                sent_code = await temp_client.send_code(clean_phone)
                state["client"] = temp_client
                state["phone"] = clean_phone
                state["phone_code_hash"] = sent_code.phone_code_hash
                state["step"] = "OTP"
                
                await msg.edit_text(
                    "✅ Code sent!\n\n"
                    "Please enter the **OTP** you received from Telegram.\n"
                    "Format: `1 2 3 4 5` (spaced) or just `12345`"
                )
            except Exception as e:
                await msg.edit_text(f"❌ Error sending code: {e}\nTry /login again.")
                await temp_client.disconnect()
                del auth_states[user_id]
                
        elif step == "OTP":
            otp = text.replace(" ", "")
            temp_client = state["client"]
            phone = state["phone"]
            hash_code = state["phone_code_hash"]
            
            msg = await message.reply_text("⏳ Verifying OTP...")
            
            try:
                await temp_client.sign_in(phone, hash_code, phone_code=otp)
                # If successful immediately
                session = await temp_client.export_session_string()
                await save_session(user_id, session, phone)
                await temp_client.disconnect()
                del auth_states[user_id]
                await msg.edit_text("✅ **Login Successful!**\nYou can now use /settings and extract features.")
                
            except Exception as e:
                err_str = str(e)
                if "PASSWORD_REQUIRED" in err_str or "SessionPasswordNeeded" in err_str or "SESSION_PASSWORD_NEEDED" in err_str:
                    state["step"] = "PASSWORD"
                    await msg.edit_text(
                        "🔐 **Two-Step Verification Found.**\n\n"
                        "Please enter your **2FA Password**."
                    )
                elif "PHONE_CODE_INVALID" in err_str:
                    await msg.edit_text("❌ **Invalid OTP.** Please try again /login.")
                    await temp_client.disconnect()
                    del auth_states[user_id]
                else:
                    await msg.edit_text(f"❌ Login Failed: {e}\nTry /login again.")
                    await temp_client.disconnect()
                    del auth_states[user_id]

        elif step == "PASSWORD":
            password = text 
            temp_client = state["client"]
            phone = state["phone"]
            
            msg = await message.reply_text("⏳ Verifying Password...")
            
            try:
                await temp_client.check_password(password=password)
                session = await temp_client.export_session_string()
                await save_session(user_id, session, phone)
                await temp_client.disconnect()
                del auth_states[user_id]
                await msg.edit_text("✅ **Login Successful!**\nYou can now use /settings and extract features.")
            except Exception as e:
                 await msg.edit_text(f"❌ Password Incorrect or Error: {e}\nTry /login again.")
                 await temp_client.disconnect()
                 del auth_states[user_id]

    except Exception as e:
        await message.reply_text(f"❌ An error occurred: {e}")
        if "client" in state:
            try:
                await state["client"].disconnect()
            except:
                pass
        del auth_states[user_id]

    return True
