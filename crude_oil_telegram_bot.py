"""
MCX Crude Oil Signal Monitor — Real-Time WhatsApp Alert Bot
============================================================
Polls the chart image every 5 seconds using async HTTP.
Only downloads the image when it actually changes (ETag/Last-Modified).
Sends WhatsApp alerts via CallMeBot (FREE) the moment a signal changes.

Author: Built for autobuysellsignal.in chart monitoring
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote

import aiohttp

# ══════════════════════════════════════════════════════════════
#  USER CONFIGURATION  ← Edit these 3 lines only
# ══════════════════════════════════════════════════════════════
WHATSAPP_NUMBER  = "+91XXXXXXXXXX"   # Your WhatsApp number with country code
CALLMEBOT_APIKEY = "YOUR_API_KEY"    # From CallMeBot (see setup guide below)
POLL_INTERVAL    = 5                 # Seconds between checks (5 = near real-time)
# ══════════════════════════════════════════════════════════════

CHART_IMAGE_URL = "https://dow.autobuysellsignal.in/CRUDEOIL-I_Chart1.png"
STATE_FILE      = "crude_bot_state.json"
LOG_FILE        = "crude_bot.log"

# ── Market hours (IST) — MCX Crude Oil: Mon–Fri 9:00 AM to 11:30 PM ──────────
MARKET_OPEN_HOUR  = 9
MARKET_CLOSE_HOUR = 23
MARKET_CLOSE_MIN  = 30

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Market hours check ────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Return True if MCX Crude Oil market is likely open (IST)."""
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    minutes = now.hour * 60 + now.minute
    open_min  = MARKET_OPEN_HOUR * 60
    close_min = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    return open_min <= minutes <= close_min


# ── OCR — reads text baked into the chart image ───────────────────────────────

def ocr_image(image_bytes: bytes) -> str:
    """Extract text from PNG image using Tesseract OCR."""
    try:
        import io
        import pytesseract
        from PIL import Image, ImageFilter, ImageEnhance

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Crop the signal box (bottom-left red panel) for faster, accurate OCR
        w, h = img.size
        signal_box = img.crop((0, int(h * 0.70), int(w * 0.45), h))

        # Enhance contrast so OCR reads white/yellow text on red background
        signal_box = signal_box.resize(
            (signal_box.width * 2, signal_box.height * 2),
            Image.LANCZOS
        )
        signal_box = ImageEnhance.Contrast(signal_box).enhance(2.5)
        signal_box = ImageEnhance.Sharpness(signal_box).enhance(2.0)

        # Tesseract config: single block of text, digits+letters only
        config = "--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz./: "
        text = pytesseract.image_to_string(signal_box, config=config)
        return text.strip()

    except ImportError:
        log.error("Missing: pip install pytesseract pillow")
        log.error("Also install Tesseract OCR engine from https://github.com/tesseract-ocr/tesseract")
        sys.exit(1)
    except Exception as e:
        log.error(f"OCR error: {e}")
        return ""


# ── Signal parser ─────────────────────────────────────────────────────────────

def parse_signal(text: str) -> dict:
    """Extract key trading fields from OCR text."""
    sig = {}
    text = text.replace("\n", " ").replace("|", " ")

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    action_m = re.search(r"(Short|Long)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if action_m:
        sig["action"] = action_m.group(1).upper()
        sig["entry"]  = action_m.group(2)

    sig["trail_sl"] = find(r"Trail\s*SL\s*([\d.]+)")
    sig["pnl"]      = find(r"P[/\\]?L[:\s]*([-\d.]+)\s*Points?")
    sig["target1"]  = find(r"Target\s*1[:\s]*([\d.]+)")
    sig["target2"]  = find(r"Target\s*2[:\s]*([\d.]+)")
    sig["target3"]  = find(r"Target\s*3[:\s]*([\d.]+)")

    for i, key in enumerate(["t1_status", "t2_status", "t3_status"], 1):
        m = re.search(rf"Target\s*{i}.*?(Achieved|Pending)", text, re.IGNORECASE)
        sig[key] = m.group(1) if m else None

    # Remove None values
    return {k: v for k, v in sig.items() if v is not None}


def detect_changes(old: dict, new: dict) -> list[str]:
    changed = []
    for k, v in new.items():
        if old.get(k) != v:
            changed.append(k)
    return changed


# ── WhatsApp message formatter ────────────────────────────────────────────────

FIELD_LABELS = {
    "action":    "Direction",
    "entry":     "Entry",
    "trail_sl":  "Trail SL",
    "pnl":       "P&L (pts)",
    "target1":   "Target 1",
    "target2":   "Target 2",
    "target3":   "Target 3",
    "t1_status": "T1 Status",
    "t2_status": "T2 Status",
    "t3_status": "T3 Status",
}

def build_message(signal: dict, changed: list[str], is_first: bool = False) -> str:
    now   = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    emoji = "🔴" if signal.get("action") == "SHORT" else "🟢"
    lines = [
        "🛢️ *MCX CRUDE OIL SIGNAL*",
        f"🕐 {now}",
        "─────────────────────",
    ]

    if "action" in signal:
        lines.append(f"{emoji} *{signal['action']}* @ ₹{signal.get('entry', '—')}")

    if "trail_sl" in signal:
        lines.append(f"🛑 Trail SL : {signal['trail_sl']}")

    if "pnl" in signal:
        pnl = float(signal["pnl"])
        p_emoji = "📈" if pnl >= 0 else "📉"
        lines.append(f"{p_emoji} P&L       : {signal['pnl']} pts")

    lines.append("─────────────────────")

    for i, (tkey, skey) in enumerate([
        ("target1", "t1_status"),
        ("target2", "t2_status"),
        ("target3", "t3_status"),
    ], 1):
        val    = signal.get(tkey, "—")
        status = signal.get(skey, "")
        badge  = "✅" if status == "Achieved" else "🎯"
        lines.append(f"{badge} T{i}: {val}   {status}")

    lines.append("─────────────────────")

    if is_first:
        lines.append("📡 *Bot connected — monitoring live*")
    elif changed:
        readable = [FIELD_LABELS.get(k, k) for k in changed]
        lines.append(f"⚡ *Updated:* {', '.join(readable)}")

    lines.append("\n_autobuysellsignal.in_")
    return "\n".join(lines)


# ── WhatsApp sender (async) ───────────────────────────────────────────────────

async def send_whatsapp(session: aiohttp.ClientSession, message: str) -> bool:
    """Send WhatsApp message via CallMeBot free API."""
    if "XXXXXXXXXX" in WHATSAPP_NUMBER or "YOUR_API_KEY" in CALLMEBOT_APIKEY:
        log.warning("⚠️  WHATSAPP_NUMBER and CALLMEBOT_APIKEY not configured!")
        log.info(f"[TEST] Would send:\n{message}")
        return False

    url = (
        f"https://api.callmebot.com/whatsapp.php"
        f"?phone={WHATSAPP_NUMBER}"
        f"&text={quote(message)}"
        f"&apikey={CALLMEBOT_APIKEY}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.text()
            if r.status == 200 and "Message Sent" in body:
                log.info("✅ WhatsApp sent!")
                return True
            log.warning(f"CallMeBot: {r.status} — {body[:150]}")
            return False
    except Exception as e:
        log.error(f"WhatsApp error: {e}")
        return False


# ── Async image fetcher with ETag support ────────────────────────────────────

async def fetch_image_if_changed(
    session: aiohttp.ClientSession,
    etag: str,
    last_modified: str,
) -> tuple[bytes | None, str, str]:
    """
    Fetch chart image only if it changed on the server.
    Returns (image_bytes_or_None, new_etag, new_last_modified).
    Uses ETag + If-None-Match for efficient polling.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer":    "https://autobuysellsignal.in/",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with session.get(
            CHART_IMAGE_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 304:
                # Server says "not modified" — no need to re-OCR
                return None, etag, last_modified

            if r.status == 200:
                data          = await r.read()
                new_etag      = r.headers.get("ETag", "")
                new_lm        = r.headers.get("Last-Modified", "")
                return data, new_etag, new_lm

            log.warning(f"Image fetch: HTTP {r.status}")
            return None, etag, last_modified

    except asyncio.TimeoutError:
        log.warning("Image fetch timed out")
        return None, etag, last_modified
    except Exception as e:
        log.error(f"Image fetch error: {e}")
        return None, etag, last_modified


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    defaults = {
        "etag": "", "last_modified": "",
        "last_hash": "", "last_signal": {},
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return defaults


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main async loop ───────────────────────────────────────────────────────────

async def monitor():
    state  = load_state()
    etag   = state["etag"]
    lm     = state["last_modified"]
    last_h = state["last_hash"]
    last_s = state["last_signal"]
    errors = 0
    first  = True

    conn    = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
        # Startup message
        await send_whatsapp(
            session,
            "🛢️ *Crude Oil Bot Started!*\n"
            f"Checking every {POLL_INTERVAL}s during MCX market hours.\n"
            "_autobuysellsignal.in_"
        )

        while True:
            try:
                if not is_market_open():
                    log.info("Market closed — sleeping 5 min")
                    await asyncio.sleep(300)
                    continue

                image_bytes, etag, lm = await fetch_image_if_changed(session, etag, lm)

                if image_bytes is None:
                    # 304 Not Modified or error — image unchanged
                    errors = 0
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Content-level hash check (in case server ignores ETag)
                current_h = hashlib.md5(image_bytes).hexdigest()
                if current_h == last_h:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                log.info("📊 Image changed → running OCR")
                text = ocr_image(image_bytes)

                if not text:
                    log.warning("OCR returned empty — skipping")
                    last_h = current_h
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_signal = parse_signal(text)
                log.info(f"Parsed: {new_signal}")

                changed = detect_changes(last_s, new_signal)

                if first and new_signal:
                    msg = build_message(new_signal, [], is_first=True)
                    await send_whatsapp(session, msg)
                    first = False
                elif changed and new_signal:
                    msg = build_message(new_signal, changed)
                    await send_whatsapp(session, msg)
                    log.info(f"Alert sent — changed: {changed}")

                last_h = current_h
                last_s = new_signal
                state.update({
                    "etag": etag, "last_modified": lm,
                    "last_hash": last_h, "last_signal": last_s,
                })
                save_state(state)
                errors = 0

            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                log.error(f"Loop error #{errors}: {e}", exc_info=True)
                if errors >= 10:
                    log.critical("10 consecutive errors — pausing 10 min")
                    await asyncio.sleep(600)
                    errors = 0

            await asyncio.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

SETUP_GUIDE = """
╔═══════════════════════════════════════════════════════════════╗
║      MCX Crude Oil — Real-Time WhatsApp Alert Bot             ║
╚═══════════════════════════════════════════════════════════════╝

📋 ONE-TIME SETUP (takes ~5 minutes):

  STEP 1 — Install Python packages:
    pip install aiohttp pytesseract pillow

  STEP 2 — Install Tesseract OCR engine:
    • Windows : https://github.com/UB-Mannheim/tesseract/wiki
    • Ubuntu  : sudo apt install tesseract-ocr
    • Mac     : brew install tesseract

  STEP 3 — Get FREE CallMeBot API key:
    a) Save  +34 644 59 90 15  in WhatsApp contacts as "CallMeBot"
    b) Send this message to that number on WhatsApp:
         I allow callmebot to send me messages
    c) You'll receive your personal API key in seconds ✅

  STEP 4 — Edit this file (only 2 lines):
    WHATSAPP_NUMBER  = "+91XXXXXXXXXX"   ← your number
    CALLMEBOT_APIKEY = "123456"          ← from step 3

  STEP 5 — Run:
    python crude_oil_whatsapp_bot.py

  STEP 6 — Keep running 24/7 (free options):
    • PythonAnywhere.com  — free Linux server, always on
    • Railway.app         — free tier, easy deploy
    • Google Colab        — keep tab open during market hours

⚡ The bot polls every 5 seconds during MCX market hours (9AM–11:30PM IST).
   It uses smart HTTP caching (ETag) so it only OCR-processes images that
   actually changed on the server — very efficient.
   Outside market hours it sleeps and checks every 5 minutes.

📲 You'll get a WhatsApp alert immediately when:
   • A new BUY or SELL signal fires
   • Entry price changes
   • Stop loss is trailed
   • A target is achieved
   • P&L updates significantly
"""

if __name__ == "__main__":
    print(SETUP_GUIDE)
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")