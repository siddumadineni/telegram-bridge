import os
import re
import sys
from pathlib import Path
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

ENV_PATH = Path(__file__).resolve().parent.parent / ".env.local"

def update_env_file(key: str, value: str):
    if not ENV_PATH.exists():
        ENV_PATH.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    content = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(content):
        new_content = pattern.sub(f"{key}={value}", content)
    else:
        new_content = content.rstrip() + f"\n{key}={value}\n"
    ENV_PATH.write_text(new_content, encoding="utf-8")

def main():
    print("=" * 60)
    print("🎬 Movie Max - Telegram One-Time Login Session Generator")
    print("=" * 60)
    print("This will connect to Telegram once to generate a StringSession.")
    print("The session string is saved in .env.local and will run headlessly.\n")

    api_id_input = input("Enter your Telegram API_ID: ").strip()
    api_hash_input = input("Enter your Telegram API_HASH: ").strip()

    if not api_id_input or not api_hash_input:
        print("❌ Error: API_ID and API_HASH are required.")
        sys.exit(1)

    try:
        api_id = int(api_id_input)
    except ValueError:
        print("❌ Error: API_ID must be a number.")
        sys.exit(1)

    print("\nConnecting to Telegram... (You will be prompted for your phone number and login code)")
    
    with TelegramClient(StringSession(), api_id, api_hash_input) as client:
        session_str = client.session.save()
        me = client.get_me()
        print("\n" + "=" * 60)
        print(f"✅ Successfully logged in as: {me.first_name} (@{me.username or me.phone})")
        print("=" * 60)
        
        # Save to .env.local automatically
        update_env_file("TELEGRAM_API_ID", str(api_id))
        update_env_file("TELEGRAM_API_HASH", api_hash_input)
        update_env_file("TELEGRAM_STRING_SESSION", session_str)
        update_env_file("TELEGRAM_MOVIE_BOT", "TGM_Files_Bot")
        update_env_file("TELEGRAM_LINK_BOT", "HyperLinkGenXBot")
        update_env_file("TELEGRAM_BRIDGE_URL", "http://127.0.0.1:5055")
        
        print("\n🎉 Saved TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_STRING_SESSION to .env.local!")
        print("You can now start the bridge server with: python telegram-bridge/server.py\n")

if __name__ == "__main__":
    main()
