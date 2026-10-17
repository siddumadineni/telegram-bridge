import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button

token_message_cache: dict[str, int] = {}

# Add current directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    yield
    if client and client.is_connected():
        await client.disconnect()
        print("🛑 Telegram Bridge disconnected.")

app = FastAPI(title="Movie Max Telegram Bridge", lifespan=lifespan)

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
    token: str
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

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Movie Max Telegram Bridge</title>
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
            <h1>🎬 Movie Max Telegram Bridge</h1>
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
async def search_movie(query: str = Query(..., min_length=2)):
    if not client or not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready. Run login.py first.")

    movie_bot_entity = await client.get_input_entity(MOVIE_BOT)

    # Send search query to Movie Bot
    print(f"🔍 Searching @{MOVIE_BOT} for: {query}")
    sent_msg = await client.send_message(movie_bot_entity, query)

    # Wait for the bot to reply with inline buttons
    reply_msg = None
    for _ in range(30):  # wait up to 15 seconds
        await asyncio.sleep(0.5)
        async for msg in client.iter_messages(movie_bot_entity, limit=5):
            if msg.id > sent_msg.id and (msg.buttons or msg.text):
                reply_msg = msg
                break
        if reply_msg and reply_msg.buttons:
            break

    if not reply_msg or not reply_msg.buttons:
        return {
            "query": query,
            "found": False,
            "message": "No matching qualities or files found from bot.",
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
                    "id": f"btn_{reply_msg.id}_{r_idx}_{c_idx}",
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

@app.post("/resolve")
async def resolve_stream(req: ResolveRequest):
    if not client or not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    movie_bot_entity = await client.get_input_entity(MOVIE_BOT)
    link_bot_entity = await client.get_input_entity(LINK_BOT)

    # 1. Fetch message and click selected button
    print(f"🎯 Clicking button [{req.row}, {req.col}] on msg {req.message_id}...")
    msg = await client.get_messages(movie_bot_entity, ids=req.message_id)
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
            sent_start = await client.send_message(movie_bot_entity, f"/start {start_param}")
            base_file_id = sent_start.id

    # 2. Wait for @TGM_Files_Bot to send the file message
    print(f"⏳ Waiting for video file from @{MOVIE_BOT}...")
    file_msg = None
    for _ in range(30):  # wait up to 15 seconds
        await asyncio.sleep(0.5)
        async for m in client.iter_messages(movie_bot_entity, limit=3):
            if m.id >= base_file_id and m.media:
                file_msg = m
                break
        if file_msg:
            break

    if not file_msg:
        raise HTTPException(status_code=504, detail="Movie bot did not send the file message in time.")

    print(f"📦 Got file message {file_msg.id}! Forwarding to @{LINK_BOT}...")

    # 3. Forward the file message to @HyperLinkGenXBot
    last_link_msgs = [m.id async for m in client.iter_messages(link_bot_entity, limit=1)]
    base_link_id = last_link_msgs[0] if last_link_msgs else 0

    await client.forward_messages(link_bot_entity, file_msg)

    # 4. Wait for @HyperLinkGenXBot to reply with the streaming/download link
    print(f"⏳ Waiting for stream link from @{LINK_BOT}...")
    link_reply = None
    for _ in range(40):  # wait up to 20 seconds
        await asyncio.sleep(0.5)
        async for m in client.iter_messages(link_bot_entity, limit=3):
            if m.id > base_link_id and not m.out:
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

    return {
        "success": True,
        "stream_url": final_stream_url,
        "download_url": download_url,
        "web_player_url": web_player_url,
        "title": req.title or "Telegram Stream",
    }

@app.post("/resolve-token")
async def resolve_token(req: TokenResolveRequest):
    if not client or not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    target_bot = (req.bot or "phonofilmbot").lstrip("@")
    target_bot_entity = await client.get_input_entity(target_bot)
    link_bot_entity = await client.get_input_entity(LINK_BOT)

    print(f"📨 Sending /start {req.token} to @{target_bot}...")
    sent = await client.send_message(target_bot_entity, f"/start {req.token}")

    print(f"⏳ Waiting for video file from @{target_bot}...")
    file_msg = None
    for _ in range(30):
        await asyncio.sleep(0.5)
        async for m in client.iter_messages(target_bot_entity, limit=3):
            if m.id >= sent.id and m.media:
                file_msg = m
                break
        if file_msg:
            break

    if not file_msg:
        raise HTTPException(status_code=504, detail=f"Bot @{target_bot} did not send file for token.")

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

@app.api_route("/stream/direct", methods=["GET", "HEAD"])
async def stream_telegram_direct(
    request: Request,
    token: str = Query(..., description="Start token for telegram bot"),
    bot: str = Query("phonofilmbot", description="Bot username")
):
    """
    Direct Telegram Stream Server (HTTP 206 Partial Content Range Requests).
    Streams chunks directly from Telegram MTProto CDN to video player without 3rd-party bots!
    """
    if not client or not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram client is not ready.")

    target_bot = bot.lstrip("@")
    target_bot_entity = await client.get_input_entity(target_bot)

    # 1. Fetch file message (or get from cache)
    file_msg = None
    if token in token_message_cache:
        cached_id = token_message_cache[token]
        file_msg = await client.get_messages(target_bot_entity, ids=cached_id)

    if not file_msg or not file_msg.media:
        print(f"📨 Triggering /start {token} on @{target_bot}...")
        sent = await client.send_message(target_bot_entity, f"/start {token}")
        for _ in range(30):
            await asyncio.sleep(0.5)
            async for m in client.iter_messages(target_bot_entity, limit=3):
                if m.id >= sent.id and m.media:
                    file_msg = m
                    token_message_cache[token] = m.id
                    break
            if file_msg:
                break

    if not file_msg or not file_msg.media:
        raise HTTPException(status_code=404, detail="File message not received from Telegram bot.")

    total_size = getattr(file_msg.file, "size", 0)
    file_name = getattr(file_msg.file, "name", f"video_{token}.mkv")

    # Content Type
    content_type = "video/mp4" if file_name.endswith(".mp4") else "video/x-matroska"

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
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range",
        "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
    }

    # If HEAD request (e.g. browser probe for duration & range support)
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)

    print(f"📡 Direct Stream chunk: bytes {start}-{end}/{total_size} ({content_length} bytes)")

    # Stream chunks on-the-fly directly from Telegram's MTProto CDN (1MB chunks for smooth buffer)
    async def chunk_generator(media, offset, limit):
        async for chunk in client.iter_download(media, offset=offset, limit=limit, chunk_size=1024 * 1024):
            yield chunk

    return StreamingResponse(
        chunk_generator(file_msg.media, start, content_length),
        status_code=206,
        headers=headers,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5055, log_level="info")
