"""
MCX Crude Oil Signal Monitor — Real-Time Telegram Alert Bot
============================================================
Polls the chart image every 5 seconds using async HTTP.
Only downloads the image when it actually changes (ETag/Last-Modified).
Sends Telegram alerts the moment a signal changes.
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
#  USER CONFIGURATION  ← Edit these 2 lines only
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8852868919:AAGC69Nd3F3LyepIMW66Do-t_HAW-bhCPoQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8755501824")
POLL_INTERVAL  = 5   # seconds between checks
# ══════════════════════════════════════════════════════════════

CHART_IMAGE_URL = "https://dow.autobuysellsignal.in/CRUDEOIL-I_Chart1.png"
STATE_FILE      = "crude_bot_state.json"
LOG_FILE        = "crude_bot.log"

MARKET_OPEN_HOUR  = 9
MARKET_CLOSE_HOUR = 23
MARKET_CLOSE_MIN  = 30

# ── Logging ───────────────────────────────────────────────────
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


# ── Market hours ──────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (MARKET_OPEN_HOUR * 60) <= minutes <= (MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN)


# ── OCR ───────────────────────────────────────────────────────

def ocr_image(image_bytes: bytes) -> str:
    try:
        import io
        import pytesseract
        from PIL import Image, ImageEnhance

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size

        # Crop just the bottom-left signal box (red panel)
        signal_box = img.crop((0, int(h * 0.70), int(w * 0.45), h))
        signal_box = signal_box.resize(
            (signal_box.width * 2, signal_box.height * 2), Image.LANCZOS
        )
        signal_box = ImageEnhance.Contrast(signal_box).enhance(2.5)
        signal_box = ImageEnhance.Sharpness(signal_box).enhance(2.0)

        config = "--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz./: "
        return pytesseract.image_to_string(signal_box, config=config).strip()

    except ImportError:
        log.error("Run: pip install pytesseract pillow")
        log.error("And: sudo apt install tesseract-ocr")
        sys.exit(1)
    except Exception as e:
        log.error(f"OCR error: {e}")
        return ""


# ── Signal parser ─────────────────────────────────────────────

def parse_signal(text: str) -> dict:
    sig  = {}
    text = text.replace("\n", " ").replace("|", " ")

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    m = re.search(r"(Short|Long)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["action"] = m.group(1).upper()
        sig["entry"]  = m.group(2)

    sig["trail_sl"] = find(r"Trail\s*SL\s*([\d.]+)")
    sig["pnl"]      = find(r"P[/\\]?L[:\s]*([-\d.]+)\s*Points?")
    sig["target1"]  = find(r"Target\s*1[:\s]*([\d.]+)")
    sig["target2"]  = find(r"Target\s*2[:\s]*([\d.]+)")
    sig["target3"]  = find(r"Target\s*3[:\s]*([\d.]+)")

    for i, key in enumerate(["t1_status", "t2_status", "t3_status"], 1):
        m = re.search(rf"Target\s*{i}.*?(Achieved|Pending)", text, re.IGNORECASE)
        sig[key] = m.group(1) if m else None

    return {k: v for k, v in sig.items() if v is not None}


def detect_changes(old: dict, new: dict) -> list:
    return [k for k, v in new.items() if old.get(k) != v]


# ── Message formatter ─────────────────────────────────────────

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

def build_message(signal: dict, changed: list, is_first: bool = False) -> str:
    now   = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    emoji = "🔴" if signal.get("action") == "SHORT" else "🟢"

    lines = [
        "🛢 *MCX CRUDE OIL SIGNAL*",
        f"🕐 {now}",
        "─────────────────────",
    ]

    if "action" in signal:
        lines.append(f"{emoji} *{signal['action']}* @ ₹{signal.get('entry', '—')}")
    if "trail_sl" in signal:
        lines.append(f"🛑 Trail SL : `{signal['trail_sl']}`")
    if "pnl" in signal:
        pnl     = float(signal["pnl"])
        p_emoji = "📈" if pnl >= 0 else "📉"
        lines.append(f"{p_emoji} P&L       : `{signal['pnl']} pts`")

    lines.append("─────────────────────")

    for i, (tkey, skey) in enumerate([
        ("target1", "t1_status"),
        ("target2", "t2_status"),
        ("target3", "t3_status"),
    ], 1):
        val    = signal.get(tkey, "—")
        status = signal.get(skey, "")
        badge  = "✅" if status == "Achieved" else "🎯"
        lines.append(f"{badge} T{i}: `{val}`   {status}")

    lines.append("─────────────────────")

    if is_first:
        lines.append("📡 *Bot connected — monitoring live*")
    elif changed:
        readable = [FIELD_LABELS.get(k, k) for k in changed]
        lines.append(f"⚡ *Updated:* {', '.join(readable)}")

    lines.append("\n_autobuysellsignal\\.in_")
    return "\n".join(lines)


# ── Telegram sender ───────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, message: str) -> bool:
    if "YOUR_BOT_TOKEN" in TELEGRAM_TOKEN:
        log.warning("Set TELEGRAM_TOKEN in environment variables!")
        log.info(f"[TEST]\n{message}")
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "MarkdownV2",
    }
    try:
        async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.json()
            if body.get("ok"):
                log.info("✅ Telegram message sent!")
                return True
            log.warning(f"Telegram error: {body}")
            return False
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False


# ── Image fetcher with ETag ───────────────────────────────────

async def fetch_image_if_changed(session, etag, last_modified):
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
            CHART_IMAGE_URL, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 304:
                return None, etag, last_modified
            if r.status == 200:
                data = await r.read()
                return data, r.headers.get("ETag", ""), r.headers.get("Last-Modified", "")
            log.warning(f"Image fetch HTTP {r.status}")
            return None, etag, last_modified
    except asyncio.TimeoutError:
        log.warning("Image fetch timed out")
        return None, etag, last_modified
    except Exception as e:
        log.error(f"Image fetch error: {e}")
        return None, etag, last_modified


# ── State ─────────────────────────────────────────────────────

def load_state() -> dict:
    defaults = {"etag": "", "last_modified": "", "last_hash": "", "last_signal": {}}
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


# ── Main loop ─────────────────────────────────────────────────

async def monitor():
    state  = load_state()
    etag   = state["etag"]
    lm     = state["last_modified"]
    last_h = state["last_hash"]
    last_s = state["last_signal"]
    errors = 0
    first  = True

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    ) as session:

        await send_telegram(
            session,
            "🛢 *MCX Crude Oil Bot Started\\!*\n"
            f"Checking every {POLL_INTERVAL}s during market hours\\.\n"
            "_autobuysellsignal\\.in_"
        )

        while True:
            try:
                if not is_market_open():
                    log.info("Market closed — sleeping 5 min")
                    await asyncio.sleep(300)
                    continue

                image_bytes, etag, lm = await fetch_image_if_changed(session, etag, lm)

                if image_bytes is None:
                    errors = 0
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                current_h = hashlib.md5(image_bytes).hexdigest()
                if current_h == last_h:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                log.info("📊 Image changed → running OCR")
                text = ocr_image(image_bytes)

                if not text:
                    log.warning("OCR empty — skipping")
                    last_h = current_h
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_signal = parse_signal(text)
                log.info(f"Parsed: {new_signal}")
                changed    = detect_changes(last_s, new_signal)

                if first and new_signal:
                    msg = build_message(new_signal, [], is_first=True)
                    await send_telegram(session, msg)
                    first = False
                elif changed and new_signal:
                    msg = build_message(new_signal, changed)
                    await send_telegram(session, msg)
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
                    log.critical("10 errors in a row — pausing 10 min")
                    await asyncio.sleep(600)
                    errors = 0

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    print("""
╔═══════════════════════════════════════════════════════╗
║   MCX Crude Oil — Real-Time Telegram Alert Bot        ║
╚═══════════════════════════════════════════════════════╝
  Chat ID : 8755501824 (already set)
  Token   : Set TELEGRAM_TOKEN environment variable
            OR paste it directly in the script

  Install : pip install aiohttp pytesseract pillow
            sudo apt install tesseract-ocr

  Run     : python crude_oil_telegram_bot.py
""")
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")
