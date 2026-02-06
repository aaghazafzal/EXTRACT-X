import asyncio
from pyrogram import Client
from dotenv import load_dotenv
import os

load_dotenv()

async def main():
    print("--- Pyrogram Session String Generator ---")
    
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not api_id or not api_hash:
        print("Error: API_ID and API_HASH not found in .env file.")
        print("Please enter them now:")
        api_id = input("Enter API_ID: ")
        api_hash = input("Enter API_HASH: ")

    print("Using in-memory session...")
    async with Client("temp_session_bot", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        print("\nSending login code...")
        # This will trigger the interactive login process in the terminal
        session_string = await app.export_session_string()
        print("\n--- YOUR SESSION STRING ---")
        print(session_string)
        print("---------------------------")
        print("Copy this string and paste it into your .env file as SESSION_STRING")

if __name__ == "__main__":
    asyncio.run(main())
