import asyncio
import logging
import asyncio
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton

# Pyrogram 2.0.106 Patch: Fix "Peer id invalid" for strictly newer 14-digit channel IDs (-10033...)
import pyrogram.utils
pyrogram.utils.MIN_CHANNEL_ID = -100999999999999

from config import API_ID, API_HASH, BOT_TOKEN
import os
from aiohttp import web
from database import init_db, update_settings, get_settings, is_user_banned, get_all_user_ids

# Plugins
from plugins.auth import handle_auth_input
from plugins.copy_manager import handle_batch_input
from plugins.settings import show_settings_panel
from plugins.livebatch import handle_livebatch_input, init_live_monitors

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Bot
bot = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=dict(root="plugins"),
    in_memory=True
)

# Text & Photo Handler for Inputs (Auth/Batch/Settings)
@bot.on_message((filters.text | filters.photo) & filters.private, group=1)
async def input_handler(client, message):
    # Check Ban Status
    if await is_user_banned(message.from_user.id):
        await message.reply_text("🚫 **Access Denied**\n\nYou are restricted from using this bot.\nContact Admin for support.")
        return

    # User Command Interceptor (Cancel Pending States)
    if message.text and message.text.startswith("/"):
        canceled = False
        if hasattr(client, "waiting_channel_user") and client.waiting_channel_user == message.from_user.id:
            del client.waiting_channel_user
            canceled = True
        if hasattr(client, "waiting_input") and client.waiting_input.get("user") == message.from_user.id:
            del client.waiting_input
            canceled = True
        
        if canceled:
            await message.reply_text("🚫 **Action Cancelled** (New command detected).")
        return

    # Check Auth Input
    if await handle_auth_input(client, message):
        return

    # Check Batch Input
    if await handle_batch_input(client, message):
        return
    
    # Check Live Batch Input
    if await handle_livebatch_input(client, message):
        return
        
    # Check Settings Add Channel Input
    # We implement simple state here for "waiting_channel" if needed
    # Or handled via a separate mechanism.
    # For now, let's implement the Add Channel input logic here simply.
    # Check Settings Input (Channel Add, Captions)
    if hasattr(client, "waiting_channel_user") and client.waiting_channel_user == message.from_user.id:
        new_channel = None
        if message.forward_from_chat:
            new_channel = str(message.forward_from_chat.id)
        elif message.text:
            new_channel = message.text.strip()
        
        if not new_channel:
            await message.reply_text("⚠️ **Invalid Input**\nPlease send a valid Channel ID, Username, or forward a message from the channel.")
            return
        
        # Proper append logic
        s = await get_settings(message.from_user.id)
        
        if not s:
            s = {"dest_channels": [], "filters": {"all": True}}
            await update_settings(message.from_user.id)
            
        current = s.get("dest_channels", [])
        
        if new_channel not in current:
            current.append(new_channel)
            await update_settings(message.from_user.id, dest_channels=current)
            await message.reply_text(f"✅ Channel `{new_channel}` added.\nTotal: {len(current)}")
        else:
             await message.reply_text(f"⚠️ Channel `{new_channel}` already exists.")
             
        del client.waiting_channel_user
        await show_settings_panel(message.from_user.id, message, is_edit=False)
        return

    # Check Caption Inputs
    if hasattr(client, "waiting_input"):
        wait_data = client.waiting_input
        if wait_data.get("user") == message.from_user.id:
            itype = wait_data.get("type")
            text = message.text or message.caption
            user_id = message.from_user.id
            
            if itype in ["rem_word", "rep_word_old", "rep_word_new", "set_prefix", "set_suffix"] and not text:
                await message.reply_text("⚠️ **Invalid Input**\nPlease send a valid text message.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_input")]]))
                return
            
            settings = await get_settings(user_id)
            if not settings: 
                settings = {"dest_channels": [], "filters": {"all": True}, "caption_rules": {}}
                await update_settings(user_id)
            
            # Robust Rules Initialization
            rules = settings.get("caption_rules") or {}
            if "removals" not in rules: rules["removals"] = []
            if "replacements" not in rules: rules["replacements"] = {}
            if "prefix" not in rules: rules["prefix"] = ""
            if "suffix" not in rules: rules["suffix"] = ""

            if itype == "rem_word":
                rules["removals"].append(text)
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Added removal rule for: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "rep_word_old":
                client.waiting_input = {"user": user_id, "type": "rep_word_new", "old_word": text}
                await message.reply_text(f"➡️ Now send the **NEW Word** to replace `{text}` with:")
                
            elif itype == "rep_word_new":
                old = wait_data.get("old_word")
                rules["replacements"][old] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Added replacement: `{old}` -> `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "set_prefix":
                rules["prefix"] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Prefix set to: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)

            elif itype == "set_suffix":
                rules["suffix"] = text
                await update_settings(user_id, caption_rules=rules)
                await message.reply_text(f"✅ Suffix set to: `{text}`")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)
                
            elif itype == "set_thumb":
                if not message.photo:
                    await message.reply_text("⚠️ **Invalid Input**\nPlease send a valid Image/Photo.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_input")]]))
                    return
                # Save the file_id of the photo
                await update_settings(user_id, custom_thumbnail=message.photo.file_id)
                await message.reply_text("✅ **Custom Thumbnail Selected!**")
                del client.waiting_input
                await show_settings_panel(user_id, message, is_edit=False)
            
            return

LANDING_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ExtractX — Private Channel Extractor by Univora</title>
  <meta name="description" content="ExtractX is a blazing-fast Telegram bot to extract, copy and forward content from private channels. Powered by Univora." />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;900&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --accent: #7c3aed;
      --accent2: #a855f7;
      --glow: rgba(124,58,237,0.45);
      --bg: #07050f;
      --card: rgba(255,255,255,0.04);
      --border: rgba(255,255,255,0.08);
      --text: #e2e2f0;
      --muted: #888aaa;
    }
    html { scroll-behavior: smooth; }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
    }
    /* Animated background orbs */
    .orb {
      position: fixed;
      border-radius: 50%;
      filter: blur(90px);
      opacity: 0.18;
      animation: drift 12s ease-in-out infinite alternate;
      pointer-events: none;
      z-index: 0;
    }
    .orb1 { width: 500px; height: 500px; background: #7c3aed; top: -100px; left: -100px; animation-delay: 0s; }
    .orb2 { width: 400px; height: 400px; background: #a855f7; bottom: -80px; right: -80px; animation-delay: 4s; }
    .orb3 { width: 300px; height: 300px; background: #ec4899; top: 40%; left: 50%; animation-delay: 8s; }
    @keyframes drift {
      from { transform: translate(0,0) scale(1); }
      to   { transform: translate(40px,30px) scale(1.1); }
    }
    nav {
      position: fixed; top: 0; left: 0; right: 0; z-index: 100;
      display: flex; align-items: center; justify-content: space-between;
      padding: 18px 40px;
      backdrop-filter: blur(20px);
      background: rgba(7,5,15,0.7);
      border-bottom: 1px solid var(--border);
    }
    .logo { font-size: 1.4rem; font-weight: 900; letter-spacing: -0.5px; }
    .logo span { color: var(--accent2); }
    .nav-btn {
      display: inline-flex; align-items: center; gap: 8px;
      background: var(--accent); color: #fff;
      padding: 10px 22px; border-radius: 50px;
      font-weight: 600; font-size: 0.9rem;
      text-decoration: none;
      transition: all 0.3s;
      box-shadow: 0 0 20px var(--glow);
    }
    .nav-btn:hover { background: var(--accent2); transform: translateY(-2px); box-shadow: 0 0 35px var(--glow); }
    /* Hero */
    .hero {
      position: relative; z-index: 1;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      text-align: center;
      min-height: 100vh; padding: 120px 24px 80px;
    }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      background: rgba(124,58,237,0.15); border: 1px solid rgba(124,58,237,0.35);
      color: var(--accent2); padding: 6px 16px; border-radius: 50px;
      font-size: 0.8rem; font-weight: 600; letter-spacing: 0.5px;
      margin-bottom: 28px;
      animation: fadeup 0.8s ease both;
    }
    .hero h1 {
      font-size: clamp(2.8rem, 7vw, 5.5rem);
      font-weight: 900; line-height: 1.05;
      letter-spacing: -2px;
      max-width: 900px;
      animation: fadeup 0.9s ease both 0.1s;
    }
    .hero h1 .grad {
      background: linear-gradient(135deg, #a855f7, #ec4899, #7c3aed);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    .hero p {
      margin-top: 22px; max-width: 580px; font-size: 1.15rem;
      color: var(--muted); line-height: 1.7;
      animation: fadeup 1s ease both 0.2s;
    }
    .hero-btns {
      display: flex; gap: 14px; margin-top: 40px; flex-wrap: wrap; justify-content: center;
      animation: fadeup 1.1s ease both 0.3s;
    }
    .btn-primary {
      display: inline-flex; align-items: center; gap: 10px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      color: #fff; padding: 16px 34px; border-radius: 50px;
      font-size: 1rem; font-weight: 700; text-decoration: none;
      box-shadow: 0 0 40px var(--glow);
      transition: all 0.3s;
    }
    .btn-primary:hover { transform: translateY(-3px); box-shadow: 0 0 60px var(--glow); }
    .btn-secondary {
      display: inline-flex; align-items: center; gap: 10px;
      background: var(--card); border: 1px solid var(--border);
      color: var(--text); padding: 16px 34px; border-radius: 50px;
      font-size: 1rem; font-weight: 600; text-decoration: none;
      transition: all 0.3s;
      backdrop-filter: blur(10px);
    }
    .btn-secondary:hover { border-color: var(--accent2); background: rgba(124,58,237,0.1); transform: translateY(-3px); }
    /* Stats bar */
    .stats-bar {
      position: relative; z-index: 1;
      display: flex; justify-content: center; gap: 0;
      background: var(--card); border: 1px solid var(--border);
      border-radius: 20px; max-width: 700px; margin: 0 auto 80px;
      backdrop-filter: blur(20px); overflow: hidden;
    }
    .stat {
      flex: 1; padding: 28px 20px; text-align: center;
      border-right: 1px solid var(--border);
    }
    .stat:last-child { border-right: none; }
    .stat-num { font-size: 2.2rem; font-weight: 900; color: var(--accent2); display: block; }
    .stat-label { font-size: 0.8rem; color: var(--muted); font-weight: 500; margin-top: 4px; }
    /* Features */
    .section { position: relative; z-index: 1; max-width: 1100px; margin: 0 auto; padding: 0 24px 100px; }
    .section-title {
      text-align: center; font-size: clamp(1.8rem,4vw,2.8rem);
      font-weight: 900; margin-bottom: 14px;
    }
    .section-sub { text-align: center; color: var(--muted); font-size: 1rem; margin-bottom: 56px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px,1fr)); gap: 24px; }
    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 20px; padding: 32px;
      backdrop-filter: blur(20px);
      transition: all 0.35s;
      position: relative; overflow: hidden;
    }
    .card::before {
      content: ''; position: absolute; inset: 0;
      background: linear-gradient(135deg, rgba(124,58,237,0.08), transparent);
      opacity: 0; transition: opacity 0.35s;
    }
    .card:hover { transform: translateY(-6px); border-color: rgba(124,58,237,0.4); box-shadow: 0 20px 60px rgba(0,0,0,0.4); }
    .card:hover::before { opacity: 1; }
    .card-icon {
      width: 52px; height: 52px; border-radius: 14px;
      background: linear-gradient(135deg, var(--accent), var(--accent2));
      display: flex; align-items: center; justify-content: center;
      font-size: 1.5rem; margin-bottom: 20px;
      box-shadow: 0 0 20px var(--glow);
    }
    .card h3 { font-size: 1.1rem; font-weight: 700; margin-bottom: 10px; }
    .card p { color: var(--muted); font-size: 0.9rem; line-height: 1.6; }
    /* CTA */
    .cta {
      position: relative; z-index: 1;
      background: linear-gradient(135deg, rgba(124,58,237,0.15), rgba(168,85,247,0.1));
      border: 1px solid rgba(124,58,237,0.3);
      border-radius: 28px; padding: 72px 40px;
      text-align: center; max-width: 800px; margin: 0 auto 100px;
      backdrop-filter: blur(20px);
    }
    .cta h2 { font-size: clamp(1.8rem,4vw,2.6rem); font-weight: 900; margin-bottom: 16px; }
    .cta p { color: var(--muted); margin-bottom: 36px; font-size: 1rem; }
    /* footer */
    footer {
      position: relative; z-index: 1;
      text-align: center; padding: 30px;
      color: var(--muted); font-size: 0.82rem;
      border-top: 1px solid var(--border);
    }
    footer a { color: var(--accent2); text-decoration: none; }
    /* Status pill */
    .status-pill {
      display: inline-flex; align-items: center; gap: 8px;
      background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.3);
      color: #6ee7b7; padding: 6px 16px; border-radius: 50px;
      font-size: 0.78rem; font-weight: 600; margin-bottom: 20px;
    }
    .pulse { width: 8px; height: 8px; background: #10b981; border-radius: 50%; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(1.4)} }
    @keyframes fadeup { from{opacity:0;transform:translateY(24px)} to{opacity:1;transform:translateY(0)} }
    @media(max-width:600px) {
      nav { padding: 14px 20px; }
      .hero { padding: 100px 20px 60px; }
      .stats-bar { flex-direction: column; margin: 0 24px 60px; }
      .stat { border-right: none; border-bottom: 1px solid var(--border); }
      .stat:last-child { border-bottom: none; }
      .cta { padding: 50px 24px; margin: 0 24px 80px; }
    }
  </style>
</head>
<body>
  <div class="orb orb1"></div>
  <div class="orb orb2"></div>
  <div class="orb orb3"></div>

  <nav>
    <div class="logo">Extract<span>X</span></div>
    <a href="https://t.me/ExtractXBot" class="nav-btn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="white"><path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.93 6.685l-1.67 7.872c-.12.546-.447.68-.905.422l-2.5-1.842-1.207 1.162c-.133.133-.245.245-.503.245l.18-2.544 4.633-4.185c.2-.18-.044-.28-.315-.1L7.06 14.54l-2.46-.768c-.535-.167-.546-.535.113-.79l9.625-3.71c.446-.162.836.108.593.413z"/></svg>
      Open Bot
    </a>
  </nav>

  <section class="hero">
    <div class="status-pill"><div class="pulse"></div> System Online &amp; Active</div>
    <div class="badge">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
      POWERED BY UNIVORA
    </div>
    <h1>Extract Anything From<br/><span class="grad">Private Channels</span></h1>
    <p>ExtractX is the most powerful Telegram bot for bulk extracting, forwarding, and managing content from private channels — with blazing speed and zero restrictions.</p>
    <div class="hero-btns">
      <a href="https://t.me/ExtractXBot" class="btn-primary">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="white"><path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.93 6.685l-1.67 7.872c-.12.546-.447.68-.905.422l-2.5-1.842-1.207 1.162c-.133.133-.245.245-.503.245l.18-2.544 4.633-4.185c-.2-.18-.044-.28-.315-.1L7.06 14.54l-2.46-.768c-.535-.167-.546-.535.113-.79l9.625-3.71c.446-.162.836.108.593.413z"/></svg>
        Launch Bot
      </a>
      <a href="https://t.me/Univora88" class="btn-secondary">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.93 6.685l-1.67 7.872c-.12.546-.447.68-.905.422l-2.5-1.842-1.207 1.162c-.133.133-.245.245-.503.245l.18-2.544 4.633-4.185c-.2-.18-.044-.28-.315-.1L7.06 14.54l-2.46-.768c-.535-.167-.546-.535.113-.79l9.625-3.71c.446-.162.836.108.593.413z"/></svg>
        Join Channel
      </a>
    </div>
  </section>

  <div class="stats-bar" style="padding:0 24px; max-width:780px;">
    <div class="stat"><span class="stat-num">24/7</span><span class="stat-label">Uptime Guaranteed</span></div>
    <div class="stat"><span class="stat-num">∞</span><span class="stat-label">Files Extractable</span></div>
    <div class="stat"><span class="stat-num">100%</span><span class="stat-label">Bypass Rate</span></div>
    <div class="stat"><span class="stat-num">v3.0</span><span class="stat-label">Current Version</span></div>
  </div>

  <section class="section">
    <div class="section-title">Everything You Need</div>
    <p class="section-sub">Professional-grade tools for power Telegram users</p>
    <div class="grid">
      <div class="card">
        <div class="card-icon">⚡</div>
        <h3>Hyper-Speed Extraction</h3>
        <p>Extract thousands of files in seconds using Telegram's native server-side copying — no local downloads.</p>
      </div>
      <div class="card">
        <div class="card-icon">🔐</div>
        <h3>Private Channel Access</h3>
        <p>Securely log in with your own account to access and extract from any private or restricted channel.</p>
      </div>
      <div class="card">
        <div class="card-icon">🎯</div>
        <h3>Smart Filters</h3>
        <p>Extract only Videos, Documents, Photos, Audio or Text. Combine filters for ultimate precision.</p>
      </div>
      <div class="card">
        <div class="card-icon">✏️</div>
        <h3>Caption Engine</h3>
        <p>Add prefix/suffix, replace words, remove phrases or strip captions entirely — all on the fly.</p>
      </div>
      <div class="card">
        <div class="card-icon">🖼️</div>
        <h3>Thumbnail Override</h3>
        <p>Set a custom thumbnail for every extracted video. Your branding, your content, your rules.</p>
      </div>
      <div class="card">
        <div class="card-icon">📡</div>
        <h3>Live Monitor</h3>
        <p>Set up auto-forwarding from any source channel. New posts get forwarded instantly, 24/7.</p>
      </div>
    </div>
  </section>

  <section style="position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:0 24px 100px;">
    <div class="cta">
      <h2>Ready to Extract?</h2>
      <p>Join thousands of users already using ExtractX to manage their Telegram content effortlessly.</p>
      <a href="https://t.me/ExtractXBot" class="btn-primary" style="font-size:1.05rem;padding:18px 44px;">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.93 6.685l-1.67 7.872c-.12.546-.447.68-.905.422l-2.5-1.842-1.207 1.162c-.133.133-.245.245-.503.245l.18-2.544 4.633-4.185c-.2-.18-.044-.28-.315-.1L7.06 14.54l-2.46-.768c-.535-.167-.546-.535.113-.79l9.625-3.71c.446-.162.836.108.593.413z"/></svg>
        Start Using ExtractX Free
      </a>
    </div>
  </section>

  <footer>
    <p>© 2026 <a href="https://t.me/Univora88">Univora</a> &mdash; ExtractX Platform. All rights reserved.</p>
    <p style="margin-top:6px;">Built with ❤️ for the Telegram community.</p>
  </footer>
</body>
</html>
"""

async def self_ping(render_url: str):
    """Pings the bot's own URL every 8 minutes to prevent Render free tier sleep."""
    import aiohttp
    await asyncio.sleep(60)  # Wait 1 min after startup before first ping
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(render_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    logger.info(f"Self-ping OK: {resp.status}")
        except Exception as e:
            logger.warning(f"Self-ping failed (will retry): {e}")
        await asyncio.sleep(480)  # Ping every 8 minutes

async def web_server():
    async def handle(request):
        return web.Response(text=LANDING_HTML, content_type="text/html", charset="utf-8")
    
    async def health(request):
        return web.Response(text='{"status":"ok","bot":"ExtractX"}', content_type="application/json")
    
    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web Server started on port {port}")

async def main():
    await init_db()
    
    # Start Web Server (For Render Port Binding)
    await web_server()

    print("Starting Bot...")
    await bot.start()
    
    # Warm up Pyrogram in-memory cache so it doesn't throw PeerIdInvalid on updates!
    print("Warming up Cache for Peer IDs...")
    try:
        async for _ in bot.get_dialogs(limit=100): pass
        print("Cache Warmed Up!")
    except Exception as e:
        print(f"Cache warmup error (Ignored): {e}")
    
    # Set Bot Menu Commands
    await bot.set_bot_commands([
        BotCommand("start", "🏠 Home"),
        BotCommand("login", "🔐 Login Account"),
        BotCommand("logout", "👋 Logout"),
        BotCommand("settings", "⚙️ Configure"),
        BotCommand("batch", "🚀 Start Copying"),
        BotCommand("livebatch", "📡 Live Monitor"),
        BotCommand("cancel", "❌ Stop Job"),
        BotCommand("showplan", "💎 My Plan"),
        BotCommand("checkcommand", "📂 All Commands"),
        BotCommand("about", "🤖 About Bot"),
        BotCommand("help", "ℹ️ Guide")
    ])
    
    print("Bot Started! Commands Set.")
    
    # Initialize Live Monitors
    print("Initializing live monitors...")
    await init_live_monitors(bot)
    print("Live monitors ready!")
    
    # Startup Notification
    try:
        print("Sending Startup Notification...")
        users = await get_all_user_ids()
        count = 0
        from pyrogram.errors import FloodWait
        for uid in users:
            try:
                await bot.send_message(
                    uid,
                    "🚀 **System Restarted & Online**\n\n"
                    "ExtractX is now live and ready for processing.\n"
                    "Tap /start to manage your tasks.\n\n"
                    "⚡ _Powered by Univora_"
                )
                count += 1
                await asyncio.sleep(0.05)
            except FloodWait as e:
                print(f"FloodWait: Sleeping {e.value}s")
                await asyncio.sleep(e.value)
            except Exception:
                pass
        print(f"Notified {count} users.")
    except Exception as e:
        print(f"Startup Notify Error: {e}")

    # Launch self-ping anti-sleep engine
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
    if "localhost" not in render_url:
        asyncio.create_task(self_ping(render_url))
        logger.info(f"Self-ping engine started → {render_url}")

    await idle()
    await bot.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
