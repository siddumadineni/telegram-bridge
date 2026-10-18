import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.custom import Button

token_message_cache: dict[str, int] = {}
token_locks: dict[str, asyncio.Lock] = {}
tgm_resolved_cache: dict[str, dict] = {}

# Add current directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ensure UTF-8 console output on Windows to prevent UnicodeEncodeError with emojis
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Load environment from .env.local
ENV_PATH = Path(__file__).resolve().parent.parent / ".env.local"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
STRING_SESSION = os.environ.get("TELEGRAM_STRING_SESSION", "")
MOVIE_BOT = os.environ.get("TELEGRAM_MOVIE_BOT", "TGM_Files_Bot").lstrip("@")
LINK_BOT = os.environ.get("TELEGRAM_LINK_BOT", "HyperLinkGenXBot").lstrip("@")

client: Optional[TelegramClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    if not API_ID or not API_HASH or not STRING_SESSION:
        print("⚠️ Warning: TELEGRAM_API_ID, TELEGRAM_API_HASH, or TELEGRAM_STRING_SESSION not configured.")
        print("Run 'python telegram-bridge/login.py' first!")
    else:
        print(f"🚀 Initializing Telegram Client for @{MOVIE_BOT} and @{LINK_BOT}...")
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ Telegram session invalid or expired. Run login.py again.")
        else:
            me = await client.get_me()
            print(f"✅ Telegram Bridge connected as: {me.first_name} (@{me.username or me.phone})")
    
    # Load and restore saved device sessions
    sessions_data = load_sessions_file()
    for s_id, s_info in sessions_data.items():
        try:
            cl = TelegramClient(StringSession(s_info["session_str"]), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                active_clients[s_id] = cl
                print(f"✅ Restored active device session {s_id} ({s_info.get('first_name', 'User')})")
        except Exception as e:
            print(f"Could not restore session {s_id}: {e}")

    yield
    for cl in list(active_clients.values()):
        if cl and cl.is_connected():
            try:
                await cl.disconnect()
            except Exception:
                pass
    if client and client.is_connected():
        await client.disconnect()
        print("🛑 Telegram Bridge disconnected.")

app = FastAPI(title="CineGram Telegram Bridge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    message_id: int
    row: int
    col: int
    title: Optional[str] = None

class TokenResolveRequest(BaseModel):
    token: Optional[str] = None
    msg_id: Optional[int] = None
    bot: Optional[str] = "phonofilmbot"
    title: Optional[str] = None

def extract_quality(text: str) -> str:
    text_lower = text.lower()
    if "4k" in text_lower or "2160p" in text_lower:
        return "4K UHD"
    if "1080p" in text_lower:
        return "1080p Full HD"
    if "720p" in text_lower:
        return "720p HD"
    if "480p" in text_lower:
        return "480p SD"
    if "360p" in text_lower:
        return "360p"
    return "HD Quality"

def extract_size(text: str) -> Optional[str]:
    match = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|MB|gb|mb))", text)
    return match.group(1).upper() if match else None

def update_env_file(key: str, value: str):
    try:
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
    except Exception as e:
        print(f"Error updating .env.local: {e}")

class SendCodeRequest(BaseModel):
    phone: str

class VerifyCodeRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: Optional[str] = None
    password: Optional[str] = None

pending_auth: dict[str, dict] = {}
active_clients: dict[str, TelegramClient] = {}
SESSIONS_FILE = Path(__file__).resolve().parent / "sessions.json"

def load_sessions_file() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_session_to_file(session_id: str, session_str: str, user_id: int, first_name: str, phone: str):
    data = load_sessions_file()
    data[session_id] = {
        "session_str": session_str,
        "user_id": user_id,
        "first_name": first_name,
        "phone": phone
    }
    try:
        SESSIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Error saving sessions.json: {e}")

async def get_client_for_session(session_id: Optional[str]) -> Optional[TelegramClient]:
    global client
    if not session_id:
        return client

    if session_id in active_clients:
        cl = active_clients[session_id]
        if not cl.is_connected():
            try:
                await cl.connect()
            except Exception:
                pass
        return cl

    sessions_data = load_sessions_file()
    if session_id in sessions_data:
        entry = sessions_data[session_id]
        try:
            cl = TelegramClient(StringSession(entry["session_str"]), API_ID, API_HASH)
            await cl.connect()
            if await cl.is_user_authorized():
                active_clients[session_id] = cl
                return cl
        except Exception as e:
            print(f"Failed to restore saved session {session_id}: {e}")

    return client

async def auto_handle_force_sub(tg_client: TelegramClient, target_bot_entity, message) -> bool:
    """
    Automatically detects and joins required channels if the bot enforces force-sub.
    """
    if not message or not message.buttons:
        return False

    joined_any = False
    for row in message.buttons:
        for btn in row:
            btn_url = getattr(btn, "url", None)
            if btn_url and ("t.me/" in btn_url or "telegram.me/" in btn_url) and "start=" not in btn_url:
                try:
                    clean = btn_url.split("t.me/")[-1].split("?")[0].strip("/")
                    if clean.startswith("+") or "joinchat" in btn_url:
                        invite_hash = clean.lstrip("+").split("/")[-1]
                        print(f"🤖 Attempting auto-join private channel: {invite_hash}...")
                        from telethon.tl.functions.messages import ImportChatInviteRequest
                        try:
                            await tg_client(ImportChatInviteRequest(invite_hash))
                            joined_any = True
                            print(f"✅ Auto-joined private channel: {invite_hash}")
                        except Exception as e:
                            print(f"Join private channel info: {e}")
                    elif clean and not clean.lower().endswith("bot"):
                        print(f"🤖 Attempting auto-join channel: @{clean}...")
                        from telethon.tl.functions.channels import JoinChannelRequest
                        try:
                            ch_ent = await tg_client.get_input_entity(clean)
                            await tg_client(JoinChannelRequest(ch_ent))
                            joined_any = True
                            print(f"✅ Auto-joined channel: @{clean}")
                        except Exception as e:
                            print(f"Join channel info: {e}")
                except Exception as e:
                    print(f"Channel parse info: {e}")

    if joined_any:
        await asyncio.sleep(1.2)
        for row in message.buttons:
            for btn in row:
                btn_txt = btn.text.lower()
                if any(k in btn_txt for k in ["join", "try again", "verify", "refresh", "continue", "done"]):
                    if getattr(btn, "data", None):
                        print(f"🔄 Clicking force-sub confirmation button: {btn.text}...")
                        try:
                            await btn.click()
                            return True
                        except Exception as e:
                            print(f"Confirmation click error: {e}")
    return joined_any

async def auto_bypass_ad_gate(tg_client: TelegramClient, target_bot_entity, message) -> bool:
    """
    Automatically detects ad-verification messages from bots (e.g. IrisFileStore60Bot / phonofilmbot),
    emulates the ad-link visit in the background, waits for the bot's verification timer,
    and automatically clicks the confirmation button to unlock the file(s).
    """
    if not message or not message.buttons:
        return False

    ad_url = None
    confirm_btn = None

    for row in message.buttons:
        for btn in row:
            b_url = getattr(btn, "url", None)
            if b_url and ("flixdex.net" in b_url or "hai8g.com" in b_url or "?a=" in b_url or "watch ads" in btn.text.lower()):
                ad_url = b_url
            elif any(k in btn.text.lower() for k in ["clicked ads", "watched ad", "verify", "i have clicked"]):
                confirm_btn = btn

    if not ad_url and not confirm_btn:
        return False

    print(f"🎬 Ad verification gate detected from bot! Target URL: {ad_url}")

    if ad_url:
        print(f"🌐 Triggering background visit to ad URL: {ad_url}...")
        try:
            def ping_ad():
                import httpx
                try:
                    with httpx.Client(timeout=4.0, follow_redirects=True) as hclient:
                        hclient.get(ad_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                except Exception:
                    pass

            await asyncio.to_thread(ping_ad)
        except Exception as e:
            print(f"Ad ping exception (non-fatal): {e}")

    # Wait 5.5 seconds for bot's ad timer
    print("⏳ Waiting 5.5 seconds for bot ad countdown...")
    await asyncio.sleep(5.5)

    if confirm_btn:
        print(f"🔄 Clicking ad confirmation button: {confirm_btn.text}...")
        try:
            res = await confirm_btn.click()
            print(f"✅ Ad verification confirmed with bot: {res}")
            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            print(f"Confirmation click exception: {e}")

    return False

async def warmup_user_session(tg_client: TelegramClient, user_name: str = "User"):
    """
    Warms up the user session immediately upon login:
    1. Sends /start to @TGM_Files_Bot, @HyperLinkGenXBot, and @phonofilmbot.
    2. Sends a sample movie search ('Inception') to @TGM_Files_Bot.
    3. If any bot demands joining channels (force-sub), auto-joins them immediately in the background.
    This guarantees zero lag and no channel-join popups when the user searches for or plays a movie.
    """
    print(f"🔥 Starting background bot warm-up for user {user_name}...")
    bots_to_start = [MOVIE_BOT, LINK_BOT, "phonofilmbot"]

    for b in bots_to_start:
        try:
            bot_entity = await tg_client.get_input_entity(b)
            print(f"📨 [Warmup] Sending /start to @{b}...")
            sent = await tg_client.send_message(bot_entity, "/start")
            await asyncio.sleep(1.0)
            async for m in tg_client.iter_messages(bot_entity, limit=3):
                if m.id >= sent.id:
                    # Check for force-sub channel buttons
                    await auto_handle_force_sub(tg_client, bot_entity, m)
                    # Check for ad gate
                    if any("clicked" in (btn.text or "").lower() or "flixdex" in (getattr(btn, "url", "") or "") for row in (m.buttons or []) for btn in row):
                        await auto_bypass_ad_gate(tg_client, bot_entity, m)
        except Exception as e:
            print(f"⚠️ [Warmup] Error starting @{b}: {e}")

    # Now send a sample search to @TGM_Files_Bot to verify and clear any force-sub gate
    try:
        tgm_entity = await tg_client.get_input_entity(MOVIE_BOT)
        print(f"🔍 [Warmup] Sending sample search 'Inception' to @{MOVIE_BOT}...")
        sample_sent = await tg_client.send_message(tgm_entity, "Inception")
        await asyncio.sleep(1.5)
        async for m in tg_client.iter_messages(tgm_entity, limit=3):
            if m.id >= sample_sent.id:
                await auto_handle_force_sub(tg_client, tgm_entity, m)
        print(f"✅ [Warmup] Bot warm-up complete for {user_name}! Channels joined and ready.")
    except Exception as e:
        print(f"⚠️ [Warmup] Sample search error: {e}")

@app.get("/auth/status")
async def auth_status(request: Request, session: Optional[str] = Query(None)):
    session_id = session or request.headers.get("x-telegram-session")
    if not session_id:
        return {
            "is_logged_in": False,
            "message": "No active session for this device. Please log in with your phone number."
        }
    cl = await get_client_for_session(session_id)
    if not cl or not cl.is_connected():
        return {
            "is_logged_in": False,
            "message": "Session not found or disconnected. Please log in."
        }
    try:
        if not await cl.is_user_authorized():
            return {
                "is_logged_in": False,
                "message": "Session unauthorized or expired. Please log in again."
            }
        me = await cl.get_me()
        return {
            "is_logged_in": True,
            "session_id": session_id,
            "user_id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name or "",
            "phone": me.phone or "",
            "username": me.username or ""
        }
    except Exception as e:
        return {"is_logged_in": False, "error": str(e)}

@app.post("/auth/send-code")
async def auth_send_code(req: SendCodeRequest):
    phone = req.phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    if len(phone) < 8:
        raise HTTPException(status_code=400, detail="Invalid phone format. Please include country code (+91...)")

    if phone in pending_auth:
        try:
            await pending_auth[phone]["client"].disconnect()
        except Exception:
            pass

    try:
        temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await temp_client.connect()
        res = await temp_client.send_code_request(phone)
        pending_auth[phone] = {
            "client": temp_client,
            "phone_code_hash": res.phone_code_hash
        }
        return {
            "success": True,
            "phone": phone,
            "phone_code_hash": res.phone_code_hash,
            "message": f"Login code sent to your Telegram app on {phone}"
        }
    except errors.FloodWaitError as fwe:
        raise HTTPException(status_code=429, detail=f"Telegram flood wait: please wait {fwe.seconds} seconds.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/verify-code")
async def auth_verify_code(req: VerifyCodeRequest):
    global client
    phone = req.phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    auth_entry = pending_auth.get(phone)
    if not auth_entry:
        raise HTTPException(status_code=400, detail="No pending login for this number. Please request a new code.")

    temp_client: TelegramClient = auth_entry["client"]
    phone_code_hash = req.phone_code_hash or auth_entry["phone_code_hash"]

    try:
        if req.password:
            await temp_client.sign_in(password=req.password)
        else:
            await temp_client.sign_in(phone=phone, code=req.code.strip(), phone_code_hash=phone_code_hash)
    except errors.SessionPasswordNeededError:
        return {
            "success": False,
            "needs_2fa": True,
            "phone": phone,
            "phone_code_hash": phone_code_hash,
            "message": "Two-factor authentication (2FA) password required."
        }
    except errors.PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="Invalid verification code. Please check your Telegram app.")
    except errors.PhoneCodeExpiredError:
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new one.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Signed in successfully!
    session_str = temp_client.session.save()
    me = await temp_client.get_me()

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    active_clients[session_id] = temp_client
    save_session_to_file(session_id, session_str, me.id, me.first_name, me.phone or phone)

    client = temp_client
    update_env_file("TELEGRAM_STRING_SESSION", session_str)

    # Automatically warm up bots & join required channels in background
    asyncio.create_task(warmup_user_session(temp_client, me.first_name))

    del pending_auth[phone]
    return {
        "success": True,
        "needs_2fa": False,
        "session_id": session_id,
        "session_string": session_str,
        "user": {
            "user_id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name or "",
            "phone": me.phone or phone,
            "username": me.username or ""
        },
        "message": f"Successfully connected to Telegram as {me.first_name}!"
    }

@app.post("/auth/logout")
async def auth_logout(request: Request, session: Optional[str] = Query(None)):
    session_id = session or request.headers.get("x-telegram-session")
    if session_id:
        if session_id in active_clients:
            cl = active_clients.pop(session_id)
            try:
                await cl.disconnect()
            except Exception:
                pass
        sessions_data = load_sessions_file()
        if session_id in sessions_data:
            del sessions_data[session_id]
            try:
                SESSIONS_FILE.write_text(json.dumps(sessions_data, indent=2), encoding="utf-8")
            except Exception:
                pass
    return {"success": True, "message": "Logged out successfully"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>CineGram Telegram Bridge</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1115; color: #fff; padding: 40px; margin: 0; }
            .card { max-width: 720px; margin: 0 auto; background: #1a1d24; border-radius: 12px; padding: 24px; border: 1px solid #2a2e39; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }
            h1 { color: #f59e0b; margin-top: 0; font-size: 24px; }
            .badge { display: inline-block; background: #065f46; color: #34d399; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 13px; }
            input { width: 70%; padding: 12px 14px; background: #0f1115; border: 1px solid #374151; color: #fff; border-radius: 6px; font-size: 15px; }
            button { padding: 12px 20px; background: #f59e0b; color: #000; border: none; font-weight: bold; border-radius: 6px; cursor: pointer; font-size: 15px; }
            button:hover { background: #d97706; }
            #results { margin-top: 24px; text-align: left; max-height: 450px; overflow-y: auto; }
            .item { background: #262b35; padding: 14px; border-radius: 8px; margin-bottom: 12px; border: 1px solid #374151; display: flex; justify-content: space-between; align-items: center; }
            .btn-play { background: #10b981; color: white; padding: 8px 16px; font-size: 13px; border-radius: 6px; }
            .btn-play:hover { background: #059669; }
            .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-right: 6px; }
            .tag-res { background: #3b82f6; color: #fff; }
            .tag-size { background: #6b7280; color: #fff; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🎬 CineGram Telegram Bridge</h1>
            <p><span class="badge">ONLINE</span> Connected to Telegram MTProto</p>
            <p style="color: #9ca3af; font-size: 14px;">Source: <b>@TGM_Files_Bot</b> &bull; Stream Engine: <b>@HyperLinkGenXBot</b></p>
            <div style="margin-top: 20px;">
                <input type="text" id="queryInput" placeholder="Enter movie title (e.g. Spider-Man Brand New Day, Avatar)..." value="Spider-Man Brand New Day" />
                <button onclick="searchMovie()">Search</button>
            </div>
            <div id="results"></div>
        </div>
        <script>
            async function searchMovie() {
                const q = document.getElementById('queryInput').value.trim();
                const resDiv = document.getElementById('results');
                if(!q) return;
                resDiv.innerHTML = '<p style="color:#f59e0b; font-weight:500;">🔍 Searching @TGM_Files_Bot for "' + q + '"... Please wait...</p>';
                try {
                    const r = await fetch('/search?query=' + encodeURIComponent(q));
                    const data = await r.json();
                    if(!data.found || !data.options.length) {
                        resDiv.innerHTML = '<p style="color:#ef4444;">No results returned by bot for: ' + q + '</p>';
                        return;
                    }
                    let html = '<h3 style="margin:16px 0 10px 0;">Available Streams (' + data.options.length + '):</h3>';
                    data.options.forEach(opt => {
                        html += `
                        <div class="item">
                            <div style="max-width: 75%;">
                                <div style="font-weight:600; font-size:14px; margin-bottom:4px;">${opt.title}</div>
                                <div>
                                    <span class="tag tag-res">${opt.quality}</span>
                                    <span class="tag tag-size">${opt.size}</span>
                                </div>
                            </div>
                            <button class="btn-play" onclick='resolveStream(${opt.message_id}, ${opt.row}, ${opt.col}, "${opt.title.replace(/"/g, '')}")'>⚡ Get Stream Link</button>
                        </div>`;
                    });
                    resDiv.innerHTML = html;
                } catch(err) {
                    resDiv.innerHTML = '<p style="color:#ef4444;">Error: ' + err.message + '</p>';
                }
            }

            async function resolveStream(message_id, row, col, title) {
                const resDiv = document.getElementById('results');
                resDiv.innerHTML = '<p style="color:#f59e0b; font-weight:500;">⏳ Requesting file & generating fast CDN stream link from @HyperLinkGenXBot... (Takes ~5-8s)</p>';
                try {
                    const r = await fetch('/resolve', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message_id, row, col, title })
                    });
                    const data = await r.json();
                    if(data.success && data.stream_url) {
                        resDiv.innerHTML = `
                        <div style="background:#064e3b; border:1px solid #10b981; padding:20px; border-radius:8px; margin-top:14px;">
                            <h3 style="margin:0 0 10px 0; color:#34d399;">🎉 Direct Stream Link Ready!</h3>
                            <p style="margin:0 0 6px 0; font-size:13px; color:#9ca3af;">Direct Download / ExoPlayer Stream URL:</p>
                            <p style="word-break:break-all; font-family:monospace; font-size:13px; background:#022c22; padding:12px; border-radius:6px; color:#a7f3d0; margin-bottom:14px;">${data.stream_url}</p>
                            <div style="display:flex; gap:10px;">
                                <a href="${data.stream_url}" target="_blank" style="padding:10px 18px; background:#10b981; color:#000; font-weight:bold; border-radius:6px; text-decoration:none;">🚀 Stream Directly</a>
                                ${data.web_player_url ? `<a href="${data.web_player_url}" target="_blank" style="padding:10px 18px; background:#3b82f6; color:#fff; font-weight:bold; border-radius:6px; text-decoration:none;">🌐 Open Web Player</a>` : ''}
                            </div>
                        </div>
                        <br/><button onclick="searchMovie()">Back to Results</button>
                        `;
                    } else {
                        resDiv.innerHTML = '<p style="color:#ef4444;">Failed: ' + JSON.stringify(data) + '</p>';
                    }
                } catch(err) {
                    resDiv.innerHTML = '<p style="color:#ef4444;">Error resolving stream: ' + err.message + '</p>';
                }
            }
        </script>
    </body>
    </html>
    """

@app.get("/health")
async def health():
    is_conn = client is not None and client.is_connected()
    return {
        "status": "ok" if is_conn else "disconnected",
        "movie_bot": f"@{MOVIE_BOT}",
        "link_bot": f"@{LINK_BOT}",
    }

@app.get("/search")
@app.get("/tgm/search")
async def search_movie(
    request: Request,
    query: str = Query(..., min_length=2),
    session: Optional[str] = Query(None)
):
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready. Run login.py first.")

    movie_bot_entity = await active_cl.get_input_entity(MOVIE_BOT)

    # Send search query to Movie Bot (@TGM_Files_Bot)
    print(f"🔍 [Bot 2] Searching @{MOVIE_BOT} for: {query}")
    sent_msg = await active_cl.send_message(movie_bot_entity, query)

    # Wait for the bot to reply with inline buttons
    reply_msg = None
    for _ in range(30):  # wait up to 15 seconds
        await asyncio.sleep(0.5)
        async for msg in active_cl.iter_messages(movie_bot_entity, limit=5):
            if msg.id > sent_msg.id and (msg.buttons or msg.text):
                reply_msg = msg
                break
        if reply_msg:
            text_lower = (reply_msg.text or "").lower()
            if "join" in text_lower or "subscribe" in text_lower or any("join" in b.text.lower() for row in (reply_msg.buttons or []) for b in row):
                print(f"🤖 @{MOVIE_BOT} requested channel join during search. Auto-joining channels...")
                joined = await auto_handle_force_sub(active_cl, movie_bot_entity, reply_msg)
                if joined:
                    print(f"🔄 Retrying search for: {query}")
                    sent_msg = await active_cl.send_message(movie_bot_entity, query)
                    reply_msg = None
                    continue
            if reply_msg.buttons:
                break

    if not reply_msg or not reply_msg.buttons:
        return {
            "query": query,
            "found": False,
            "message": "No matching qualities or files found from Bot 2.",
            "options": []
        }

    options = []
    for r_idx, row in enumerate(reply_msg.buttons):
        for c_idx, btn in enumerate(row):
            btn_text = btn.text.strip()
            # Ignore navigation buttons or ads
            if any(w in btn_text.lower() for w in ["next", "back", "cancel", "close", "home", ">>", "<<", "page", "unlock premium", "audios", "resolutions", "seasons", "trending"]):
                continue

            # Only include rows that have actual movie qualities or sizes
            if any(k in btn_text.lower() for k in ["gb", "mb", "1080p", "720p", "480p", "4k", "mkv", "mp4"]):
                options.append({
                    "id": f"tgm_{reply_msg.id}_{r_idx}_{c_idx}",
                    "message_id": reply_msg.id,
                    "row": r_idx,
                    "col": c_idx,
                    "title": btn_text,
                    "quality": extract_quality(btn_text),
                    "size": extract_size(btn_text) or "Direct Stream",
                })

    return {
        "query": query,
        "found": len(options) > 0,
        "message_id": reply_msg.id,
        "options": options,
    }

@app.post("/tgm/resolve")
async def tgm_resolve(
    req: ResolveRequest,
    request: Request,
    session: Optional[str] = Query(None)
):
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    movie_bot_entity = await active_cl.get_input_entity(MOVIE_BOT)
    msg = await active_cl.get_messages(movie_bot_entity, ids=req.message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Original button message not found.")

    cb_res = await msg.click(req.row, req.col)
    base_file_id = req.message_id
    if cb_res and getattr(cb_res, "url", None) and "start=" in cb_res.url:
        parsed = urlparse(cb_res.url)
        start_param = parse_qs(parsed.query).get("start", [None])[0]
        if start_param:
            sent_start = await active_cl.send_message(movie_bot_entity, f"/start {start_param}")
            base_file_id = sent_start.id

    file_msg = None
    for _ in range(30):
        await asyncio.sleep(0.5)
        async for m in active_cl.iter_messages(movie_bot_entity, limit=4):
            if m.id >= base_file_id and getattr(m, "file", None) and (m.file.size or 0) > 15_000_000:
                file_msg = m
                break
        if file_msg:
            break

    if not file_msg:
        raise HTTPException(status_code=504, detail="Movie file not received from Bot 2 in time.")

    host = request.headers.get("host", "127.0.0.1:5055")
    sess_param = f"&session={session_id}" if session_id else ""
    direct_url = f"http://{host}/stream/direct?msg_id={file_msg.id}&bot={MOVIE_BOT}{sess_param}"

    return {
        "success": True,
        "message_id": file_msg.id,
        "file_name": getattr(file_msg.file, "name", req.title or "Movie"),
        "file_size": file_msg.file.size,
        "direct_stream_url": direct_url,
        "stream_url": direct_url
    }

@app.post("/resolve")
@app.post("/tgm/resolve")
async def resolve_stream(req: ResolveRequest, request: Request, session: Optional[str] = Query(None)):
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    cache_key = f"{req.message_id}_{req.row}_{req.col}"
    if cache_key in tgm_resolved_cache:
        print(f"⚡ Returning cached stream URL for {cache_key}: {tgm_resolved_cache[cache_key].get('stream_url')}")
        return tgm_resolved_cache[cache_key]

    movie_bot_entity = await active_cl.get_input_entity(MOVIE_BOT)
    link_bot_entity = await active_cl.get_input_entity(LINK_BOT)

    # 1. Fetch message and click selected button
    print(f"🎯 Clicking button [{req.row}, {req.col}] on msg {req.message_id}...")
    msg = await active_cl.get_messages(movie_bot_entity, ids=req.message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Original button message not found.")

    cb_res = await msg.click(req.row, req.col)

    base_file_id = req.message_id
    # If the bot returned a callback URL with ?start=file_xxx, we need to send /start <param>
    if cb_res and getattr(cb_res, "url", None) and "start=" in cb_res.url:
        parsed = urlparse(cb_res.url)
        start_param = parse_qs(parsed.query).get("start", [None])[0]
        if start_param:
            print(f"📨 Sending /start {start_param} to @{MOVIE_BOT}...")
            sent_start = await active_cl.send_message(movie_bot_entity, f"/start {start_param}")
            base_file_id = sent_start.id

    # 2. Wait for @TGM_Files_Bot to send the file message
    print(f"⏳ Waiting for video file from @{MOVIE_BOT}...")
    file_msg = None
    for _ in range(30):  # wait up to 15 seconds
        await asyncio.sleep(0.5)
        async for m in active_cl.iter_messages(movie_bot_entity, limit=3):
            if m.id >= base_file_id:
                if m.media:
                    file_msg = m
                    break
                elif m.buttons:
                    text_lower = (m.text or "").lower()
                    if "join" in text_lower or "subscribe" in text_lower or any("join" in b.text.lower() for row in m.buttons for b in row):
                        print("🤖 Force-sub channel join requested! Auto-joining channels...")
                        await auto_handle_force_sub(active_cl, movie_bot_entity, m)
        if file_msg:
            break

    if not file_msg:
        raise HTTPException(status_code=504, detail="Movie bot did not send the file message in time.")

    print(f"📦 Got file message {file_msg.id}! Forwarding to @{LINK_BOT}...")

    # 3. Forward the file message to @HyperLinkGenXBot
    last_link_msgs = [m.id async for m in active_cl.iter_messages(link_bot_entity, limit=1)]
    base_link_id = last_link_msgs[0] if last_link_msgs else 0

    await active_cl.forward_messages(link_bot_entity, file_msg)

    print(f"⏳ Waiting for stream link from @{LINK_BOT}...")
    link_reply = None
    for _ in range(40):  # wait up to 20 seconds
        await asyncio.sleep(0.5)
        async for m in active_cl.iter_messages(link_bot_entity, limit=3):
            if m.id > base_link_id and not m.out:
                text_lower = (m.text or "").lower()
                if "daily limit reached" in text_lower or "exhausted your daily limit" in text_lower:
                    print("⚠️ HyperLink bot free daily limit reached (12/12)!")
                    raise HTTPException(status_code=429, detail="HyperLink bot free daily limit reached (12/12). Please use Direct Stream!")
                if (m.buttons and any(getattr(b, "url", None) for row in m.buttons for b in row)) or ("Download Link" in (m.text or "")):
                    link_reply = m
                    break
        if link_reply:
            break

    if not link_reply:
        raise HTTPException(status_code=504, detail="File-to-link bot did not respond in time.")

    # 5. Extract Direct Download URL and Web Player URL
    download_url = None
    web_player_url = None

    if link_reply.buttons:
        for row in link_reply.buttons:
            for btn in row:
                btn_url = getattr(btn, "url", None)
                if btn_url:
                    if "stream" in btn.text.lower() or "play" in btn.text.lower():
                        web_player_url = btn_url
                    elif "download" in btn.text.lower():
                        download_url = btn_url

    if link_reply.text:
        urls = re.findall(r"https?://[^\s<>\"'`]+", link_reply.text)
        for u in urls:
            if "download" in u and not download_url:
                download_url = u
            elif ("stream" in u or "play" in u) and not web_player_url:
                web_player_url = u

    final_stream_url = download_url or web_player_url
    if not final_stream_url:
        raise HTTPException(status_code=502, detail="Failed to extract streaming URL from link bot reply.")

    print(f"✨ Successfully generated stream link: {final_stream_url}")

    res = {
        "success": True,
        "stream_url": final_stream_url,
        "download_url": download_url,
        "web_player_url": web_player_url,
        "title": req.title or "Telegram Stream",
    }
    tgm_resolved_cache[cache_key] = res
    return res

@app.post("/resolve-token")
async def resolve_token(req: TokenResolveRequest):
    if not client or not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    target_bot = (req.bot or "phonofilmbot").lstrip("@")
    target_bot_entity = await client.get_input_entity(target_bot)
    link_bot_entity = await client.get_input_entity(LINK_BOT)

    file_msg = None
    if req.msg_id:
        print(f"📦 Direct message ID provided: {req.msg_id} on @{target_bot}")
        file_msg = await client.get_messages(target_bot_entity, ids=req.msg_id)
    elif req.token:
        print(f"📨 Sending /start {req.token} to @{target_bot}...")
        sent = await client.send_message(target_bot_entity, f"/start {req.token}")

        print(f"⏳ Waiting for video file from @{target_bot}...")
        for _ in range(35):
            await asyncio.sleep(0.5)
            async for m in client.iter_messages(target_bot_entity, limit=4):
                if m.id >= sent.id:
                    if getattr(m, "file", None):
                        file_msg = m
                        break
                    elif m.buttons and not file_msg:
                        for row in m.buttons:
                            for btn in row:
                                btn_url = getattr(btn, "url", None)
                                if btn_url and "start=" in btn_url:
                                    parsed = urlparse(btn_url)
                                    sub_token = parse_qs(parsed.query).get("start", [None])[0]
                                    if sub_token:
                                        print(f"🔄 Following resolution button: {btn.text} -> /start {sub_token}")
                                        sent = await client.send_message(target_bot_entity, f"/start {sub_token}")
                                        break
            if file_msg and getattr(file_msg, "file", None):
                break

    if not file_msg:
        raise HTTPException(status_code=504, detail=f"Bot @{target_bot} did not send file for token/msg_id.")

    print(f"📦 Got file {file_msg.id} ({getattr(file_msg.file, 'name', 'unknown')}). Forwarding to @{LINK_BOT}...")
    last_link_msgs = [m.id async for m in client.iter_messages(link_bot_entity, limit=1)]
    base_link_id = last_link_msgs[0] if last_link_msgs else 0

    await client.forward_messages(link_bot_entity, file_msg)

    print(f"⏳ Waiting for stream link from @{LINK_BOT}...")
    link_reply = None
    for _ in range(40):
        await asyncio.sleep(0.5)
        async for m in client.iter_messages(link_bot_entity, limit=3):
            if m.id > base_link_id and not m.out:
                text_lower = (m.text or "").lower()
                if "daily limit reached" in text_lower or "exhausted your daily limit" in text_lower:
                    print("⚠️ HyperLink bot free daily limit reached (12/12)!")
                    raise HTTPException(status_code=429, detail="HyperLink bot free daily limit reached (12/12). Please use Direct Stream!")
                if (m.buttons and any(getattr(b, "url", None) for row in m.buttons for b in row)) or ("Download Link" in (m.text or "")):
                    link_reply = m
                    break
        if link_reply:
            break

    if not link_reply:
        raise HTTPException(status_code=504, detail="File-to-link bot did not respond in time.")

    download_url = None
    web_player_url = None

    if link_reply.buttons:
        for row in link_reply.buttons:
            for btn in row:
                btn_url = getattr(btn, "url", None)
                if btn_url:
                    if "stream" in btn.text.lower() or "play" in btn.text.lower():
                        web_player_url = btn_url
                    elif "download" in btn.text.lower():
                        download_url = btn_url

    if link_reply.text:
        urls = re.findall(r"https?://[^\s<>\"'`]+", link_reply.text)
        for u in urls:
            if "download" in u and not download_url:
                download_url = u
            elif ("stream" in u or "play" in u) and not web_player_url:
                web_player_url = u

    final_stream_url = download_url or web_player_url
    if not final_stream_url:
        raise HTTPException(status_code=502, detail="Failed to extract streaming URL from link bot reply.")

    print(f"✨ Successfully generated stream link: {final_stream_url}")

    return {
        "success": True,
        "stream_url": final_stream_url,
        "download_url": download_url,
        "web_player_url": web_player_url,
        "file_name": getattr(file_msg.file, "name", req.title or "Movie"),
        "file_size": file_msg.file.size if file_msg.file else None,
    }

series_episodes_cache: Dict[str, List[dict]] = {}

@app.get("/series/episodes")
async def get_series_episodes(
    request: Request,
    token: str = Query(..., description="Season start token"),
    bot: str = Query("IrisFileStore60Bot", description="Bot username"),
    season: Optional[int] = Query(None, description="Season number (e.g. 1, 2)"),
    session: Optional[str] = Query(None, description="Device session ID")
):
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    target_bot = bot.lstrip("@")
    cache_key = f"{target_bot}_{token}_{season or 0}"
    if cache_key in series_episodes_cache:
        return {"success": True, "bot": target_bot, "season_token": token, "episodes": series_episodes_cache[cache_key]}

    target_bot_entity = await active_cl.get_input_entity(target_bot)
    host = request.headers.get("host", "127.0.0.1:5055")
    s_filter = rf"s0?{season}e\d+" if season else r"s\d+e\d+"

    episode_msgs = []
    # 1. Fast check: Check if recent messages already contain episodes for this season
    async for m in active_cl.iter_messages(target_bot_entity, limit=100):
        if m.file and (m.file.size or 0) > 10_000_000:
            name = getattr(m.file, "name", None) or (m.text or "")
            if re.search(s_filter, name, re.IGNORECASE):
                episode_msgs.append(m)

    # 2. If not found in chat, trigger /start {token}
    if not episode_msgs:
        print(f"📨 Triggering season /start {token} on @{target_bot}...")
        sent = await active_cl.send_message(target_bot_entity, f"/start {token}")
        clicked_res_button = False
        for _ in range(40):
            await asyncio.sleep(0.5)
            async for m in active_cl.iter_messages(target_bot_entity, limit=25):
                if m.id >= sent.id:
                    if m.buttons:
                        await auto_handle_force_sub(active_cl, target_bot_entity, m)
                        if any("clicked" in b.text.lower() or "flixdex" in (getattr(b, "url", "") or "") for row in m.buttons for b in row):
                            await auto_bypass_ad_gate(active_cl, target_bot_entity, m)

                    if m.buttons and not clicked_res_button and not episode_msgs:
                        chosen_btn = None
                        for row in m.buttons:
                            for btn in row:
                                b_text = btn.text.lower()
                                b_url = getattr(btn, "url", None)
                                if b_url and "start=" in b_url:
                                    if "1080p" in b_text:
                                        chosen_btn = btn
                                        break
                                    elif "720p" in b_text and not chosen_btn:
                                        chosen_btn = btn
                                    elif not chosen_btn:
                                        chosen_btn = btn
                            if chosen_btn and ("1080p" in chosen_btn.text.lower() or "720p" in chosen_btn.text.lower()):
                                break
                        if chosen_btn:
                            parsed = urlparse(getattr(chosen_btn, "url", ""))
                            sub_token = parse_qs(parsed.query).get("start", [None])[0]
                            if sub_token:
                                print(f"🔄 Clicking resolution {chosen_btn.text} -> /start {sub_token}")
                                clicked_res_button = True
                                sent = await active_cl.send_message(target_bot_entity, f"/start {sub_token}")
                                break
                    elif getattr(m, "file", None) and (m.file.size or 0) > 10_000_000:
                        name = getattr(m.file, "name", None) or (m.text or "")
                        if re.search(s_filter, name, re.IGNORECASE) and not any(x.id == m.id for x in episode_msgs):
                            episode_msgs.append(m)

            if len(episode_msgs) > 0:
                await asyncio.sleep(2.0)
                async for m in active_cl.iter_messages(target_bot_entity, limit=40):
                    if m.id >= sent.id and getattr(m, "file", None) and (m.file.size or 0) > 10_000_000:
                        name = getattr(m.file, "name", None) or (m.text or "")
                        if re.search(s_filter, name, re.IGNORECASE) and not any(x.id == m.id for x in episode_msgs):
                            episode_msgs.append(m)
                break

    if not episode_msgs:
        raise HTTPException(status_code=404, detail="No episode files found for this season.")

    by_ep: Dict[int, dict] = {}
    for m in episode_msgs:
        f_name = getattr(m.file, "name", None) or (m.text or "").split("\n")[0].strip() or f"Episode_{m.id}.mkv"
        f_size = getattr(m.file, "size", 0)

        ep_match = re.search(r"(?:s\d+e|ep(?:isode)?\.?\s*|e)(\d{1,3})", f_name, re.IGNORECASE)
        ep_num = int(ep_match.group(1)) if ep_match else len(by_ep) + 1

        clean_title = f_name.replace("- T4TSA.cc", "").replace(".mkv", "").replace(".mp4", "").replace("_", " ").strip()
        quality_str = extract_quality(f_name)
        size_gb = f"{f_size / (1024**3):.2f} GB" if f_size > 1024**3 else f"{f_size / (1024**2):.0f} MB"

        ep_info = {
            "episode_number": ep_num,
            "title": f"Episode {ep_num} - {clean_title}",
            "file_name": f_name,
            "file_size": f_size,
            "formatted_size": size_gb,
            "quality": quality_str,
            "message_id": m.id,
            "bot": target_bot,
            "token": token,
            "direct_stream_url": f"http://{host}/stream/direct?msg_id={m.id}&bot={target_bot}"
        }

        if ep_num not in by_ep or f_size > by_ep[ep_num]["file_size"]:
            by_ep[ep_num] = ep_info

    parsed_episodes = sorted(by_ep.values(), key=lambda x: x["episode_number"])
    series_episodes_cache[cache_key] = parsed_episodes


class TGMResolveRequest(BaseModel):
    message_id: int
    row: int
    col: int
    title: Optional[str] = None
    session: Optional[str] = None

@app.get("/tgm/search")
async def tgm_search(
    request: Request,
    query: str = Query(..., description="Movie or series title to search in TG Movies 4U"),
    session: Optional[str] = Query(None, description="Device session ID")
):
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    bot_username = "TGM_Files_Bot"
    try:
        bot_entity = await active_cl.get_input_entity(bot_username)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Bot @{bot_username} not found: {e}")

    # Clean query to provide exact movie title
    clean_q = re.sub(r"\s*\(\d{4}\).*", "", query).strip()
    clean_q = re.sub(r"\s+\d{4}$", "", clean_q).strip()
    search_term = clean_q if clean_q else query.strip()

    print(f"🔍 Searching TG Movies 4U (@{bot_username}) for: '{search_term}'...")
    try:
        sent = await active_cl.send_message(bot_entity, search_term)
    except errors.FloodWaitError as fwe:
        raise HTTPException(status_code=429, detail=f"Telegram FloodWait: please wait {fwe.seconds}s.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sending query: {e}")

    # Wait for bot response with buttons
    reply_msg = None
    for _ in range(12):
        await asyncio.sleep(0.5)
        recent = await active_cl.get_messages(bot_entity, limit=4)
        for m in recent:
            if m.id > sent.id and m.reply_markup and hasattr(m.reply_markup, 'rows'):
                reply_msg = m
                break
        if reply_msg:
            break

    if not reply_msg or not reply_msg.reply_markup or not hasattr(reply_msg.reply_markup, 'rows'):
        return {"found": False, "query": search_term, "options": []}

    options = []
    for r_idx, row in enumerate(reply_msg.reply_markup.rows):
        for c_idx, btn in enumerate(row.buttons):
            text = btn.text.strip()
            txt_low = text.lower()
            if any(k in txt_low for k in ["unlock", "audio", "resolution", "season", "collection", "send all", "trending", "next", "prev", "page", "close", "join", "flick"]):
                continue

            size = extract_size(text)
            if not size:
                continue

            quality = extract_quality(text)

            options.append({
                "id": f"{reply_msg.id}_{r_idx}_{c_idx}",
                "message_id": reply_msg.id,
                "row": r_idx,
                "col": c_idx,
                "title": text,
                "quality": quality,
                "size": size,
                "bot": bot_username
            })

    return {
        "found": len(options) > 0,
        "query": search_term,
        "message_id": reply_msg.id,
        "bot": bot_username,
        "options": options
    }

@app.post("/tgm/resolve")
async def tgm_resolve(
    req: TGMResolveRequest,
    request: Request
):
    session_id = req.session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    bot_username = "TGM_Files_Bot"
    try:
        bot_entity = await active_cl.get_input_entity(bot_username)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Bot @{bot_username} not found: {e}")

    try:
        target_msg = await active_cl.get_messages(bot_entity, ids=req.message_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Search message not found: {e}")

    if not target_msg or not target_msg.reply_markup:
        raise HTTPException(status_code=404, detail="Target message or buttons expired.")

    print(f"🔄 Resolving TG Movies 4U stream: message {req.message_id}, row {req.row}, col {req.col}...")
    try:
        click_res = await target_msg.click(req.row, req.col)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to click button: {e}")

    # Check if click returned a URL with start= token
    start_token = None
    url = getattr(click_res, "url", None)
    if url and "start=" in url:
        parsed = urlparse(url)
        start_token = parse_qs(parsed.query).get("start", [None])[0]

    file_msg = None
    if start_token:
        print(f"🚀 Sending start token to @{bot_username}: /start {start_token}...")
        sent_start = await active_cl.send_message(bot_entity, f"/start {start_token}")
        for _ in range(14):
            await asyncio.sleep(0.5)
            recent = await active_cl.get_messages(bot_entity, limit=4)
            for m in recent:
                if m.id > sent_start.id and getattr(m, "file", None):
                    file_msg = m
                    break
            if file_msg:
                break
    else:
        for _ in range(8):
            await asyncio.sleep(0.5)
            recent = await active_cl.get_messages(bot_entity, limit=4)
            for m in recent:
                if m.id > target_msg.id and getattr(m, "file", None):
                    file_msg = m
                    break
            if file_msg:
                break

    if not file_msg or not file_msg.file:
        raise HTTPException(status_code=404, detail="Could not retrieve video file from @TGM_Files_Bot.")

    host = request.headers.get("host", "127.0.0.1:5055")
    sess_param = f"&session={session_id}" if session_id else ""
    direct_url = f"http://{host}/stream/direct?msg_id={file_msg.id}&bot={bot_username}{sess_param}"

    return {
        "success": True,
        "bot": bot_username,
        "message_id": file_msg.id,
        "file_name": getattr(file_msg.file, "name", "video.mkv"),
        "file_size": getattr(file_msg.file, "size", 0),
        "direct_url": direct_url
    }

@app.api_route("/stream/direct", methods=["GET", "HEAD"])

async def stream_telegram_direct(
    request: Request,
    token: Optional[str] = Query(None, description="Start token for telegram bot"),
    msg_id: Optional[int] = Query(None, description="Direct Telegram message ID in bot chat"),
    bot: str = Query("phonofilmbot", description="Bot username"),
    session: Optional[str] = Query(None, description="Device session ID")
):
    """
    Direct Telegram Stream Server (HTTP 206 Partial Content Range Requests).
    Streams chunks directly from Telegram MTProto CDN to video player without 3rd-party bots!
    """
    session_id = session or request.headers.get("x-telegram-session")
    active_cl = await get_client_for_session(session_id)
    if not active_cl or not active_cl.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    target_bot = bot.lstrip("@")
    target_bot_entity = await active_cl.get_input_entity(target_bot)

    # 1. Fetch file message (by direct message ID or token)
    file_msg = None
    if msg_id:
        file_msg = await active_cl.get_messages(target_bot_entity, ids=msg_id)
    elif token:
        if token in token_message_cache:
            cached_id = token_message_cache[token]
            file_msg = await active_cl.get_messages(target_bot_entity, ids=cached_id)

        if not file_msg or not getattr(file_msg, "file", None):
            if token not in token_locks:
                token_locks[token] = asyncio.Lock()

            async with token_locks[token]:
                if token in token_message_cache:
                    cached_id = token_message_cache[token]
                    file_msg = await active_cl.get_messages(target_bot_entity, ids=cached_id)

                if not file_msg or not getattr(file_msg, "file", None):
                    try:
                        print(f"📨 Triggering /start {token} on @{target_bot}...")
                        sent = await active_cl.send_message(target_bot_entity, f"/start {token}")
                    except errors.FloodWaitError as fwe:
                        print(f"⚠️ Telegram FloodWait: {fwe.seconds}s wait required.")
                        raise HTTPException(status_code=429, detail=f"Telegram rate limited: wait {fwe.seconds}s.")
                    except Exception as e:
                        raise HTTPException(status_code=502, detail=f"Telegram error: {e}")

                    is_ad_locked = False
                    ad_verify_url = None
                    followed_tokens = set()

                    for _ in range(30):
                        await asyncio.sleep(0.5)
                        async for m in active_cl.iter_messages(target_bot_entity, limit=4):
                            if m.id >= sent.id:
                                # Only accept real video files (> 15 MB), never ad tutorial videos (768 KB)
                                if getattr(m, "file", None) and (m.file.size or 0) > 15_000_000:
                                    file_msg = m
                                    token_message_cache[token] = m.id
                                    print(f"✅ Found matching movie file for token {token}: {getattr(m.file, 'name', 'unnamed')} ({m.file.size} bytes)")
                                    break
                                elif m.buttons and not file_msg:
                                    # Check if the bot sent an ad verification requirement
                                    if m.text and ("watch an ad" in m.text.lower() or "tutorial" in m.text.lower()):
                                        is_ad_locked = True

                                    # Automatically join channels if the bot enforces force-sub
                                    await auto_handle_force_sub(active_cl, target_bot_entity, m)

                                    # Automatically bypass ad-gate if present
                                    if any("clicked" in b.text.lower() or "flixdex" in (getattr(b, "url", "") or "") for row in m.buttons for b in row):
                                        await auto_bypass_ad_gate(active_cl, target_bot_entity, m)

                                    for row in m.buttons:
                                        for btn in row:
                                            btn_url = getattr(btn, "url", None)
                                            if btn_url and "flixdex.net" in btn_url:
                                                is_ad_locked = True
                                                ad_verify_url = btn_url
                                            elif btn_url and "start=" in btn_url:
                                                parsed = urlparse(btn_url)
                                                sub_token = parse_qs(parsed.query).get("start", [None])[0]
                                                if sub_token and sub_token not in followed_tokens:
                                                    followed_tokens.add(sub_token)
                                                    print(f"🔄 Following resolution button once: {btn.text} -> /start {sub_token}")
                                                    try:
                                                        sent = await active_cl.send_message(target_bot_entity, f"/start {sub_token}")
                                                    except errors.FloodWaitError as fwe:
                                                        print(f"⚠️ Telegram FloodWait on sub-token: {fwe.seconds}s.")
                                                    break
                        if file_msg and getattr(file_msg, "file", None):
                            break

    if not file_msg or not file_msg.media:
        if is_ad_locked:
            raise HTTPException(
                status_code=403,
                detail=f"This file is locked behind Telegram bot's 5-second ad verification ({ad_verify_url or 'flixdex.net'}). flixdex.net is currently experiencing server downtime (502 Bad Gateway). Please select another quality or stream source."
            )
        raise HTTPException(status_code=404, detail="File message not received from Telegram bot.")

    total_size = getattr(file_msg.file, "size", 0)
    file_name = getattr(file_msg.file, "name", None) or f"video_{msg_id or token or 'stream'}.mkv"

    # Content Type (safe against None)
    content_type = "video/mp4" if (file_name and file_name.lower().endswith(".mp4")) else "video/x-matroska"

    # Parse HTTP Range Header
    range_header = request.headers.get("range")
    start = 0
    end = total_size - 1

    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))

    # Guard against invalid ranges
    if start >= total_size:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable")

    content_length = end - start + 1

    headers = {
        "Content-Range": f"bytes {start}-{end}/{total_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Type": content_type,
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=60, max=1000",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range",
        "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
    }

    # If HEAD request (e.g. browser probe for duration & range support)
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)

    print(f"📡 Direct Stream chunk: bytes {start}-{end}/{total_size} ({content_length} bytes)")

    # Stream chunks on-the-fly directly from Telegram's MTProto CDN with prefetch buffering
    async def chunk_generator(media, req_start, req_length):
        # MTProto requires chunk_size and request_size to be multiples of 4KB (4096 bytes)
        # 128 KB is the official Telegram part size and works reliably across all file boundaries
        CHUNK_SIZE = 128 * 1024
        aligned_offset = (req_start // 4096) * 4096
        initial_skip = req_start - aligned_offset

        queue = asyncio.Queue(maxsize=48)  # Prefetch buffer of up to 6MB in memory
        stop_event = asyncio.Event()

        async def prefetch_worker():
            try:
                stream = active_cl.iter_download(media, offset=aligned_offset, chunk_size=CHUNK_SIZE, request_size=CHUNK_SIZE)
                rem = req_length
                skip = initial_skip
                async for chunk in stream:
                    if stop_event.is_set() or rem <= 0:
                        break
                    if skip > 0:
                        if len(chunk) <= skip:
                            skip -= len(chunk)
                            continue
                        else:
                            chunk = chunk[skip:]
                            skip = 0
                    if len(chunk) > rem:
                        chunk = chunk[:rem]
                    rem -= len(chunk)
                    await queue.put(chunk)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Prefetch worker exception: {e}")
            finally:
                try:
                    await stream.close()
                except Exception:
                    pass
                await queue.put(None)

        worker = asyncio.create_task(prefetch_worker())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            stop_event.set()
            worker.cancel()

    return StreamingResponse(
        chunk_generator(file_msg.media, start, content_length),
        status_code=206,
        headers=headers,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5055, log_level="info")
