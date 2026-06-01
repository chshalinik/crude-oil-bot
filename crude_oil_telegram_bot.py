import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime

import aiohttp

# ══════════════════════════════════════════════════════════════
#  USER CONFIGURATION
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8755501824")
POLL_INTERVAL    = 5    # seconds between checks
PRICE_OFFSET     = -2   # subtract 2 from all price values
# ══════════════════════════════════════════════════════════════

CHART_IMAGE_URL = "https://dow.autobuysellsignal.in/CRUDEOIL-I_Chart1.png"
STATE_FILE      = "crude_bot_state.json"
LOG_FILE        = "crude_bot.log"

MARKET_OPEN_HOUR  = 9
MARKET_CLOSE_HOUR = 23
MARKET_CLOSE_MIN  = 30

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
]

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


# ── Price adjustment ──────────────────────────────────────────

def adjust(value_str: str) -> str:
    try:
        val      = float(value_str)
        adjusted = val + PRICE_OFFSET
        decimals = len(value_str.split(".")[-1]) if "." in value_str else 0
        return f"{adjusted:.{decimals}f}"
    except Exception:
        return value_str


# ── Market hours ──────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (MARKET_OPEN_HOUR * 60) <= minutes <= (MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN)


# ── OCR ───────────────────────────────────────────────────────

def ocr_image(image_bytes: bytes) -> dict:
    try:
        import io
        import pytesseract
        from PIL import Image, ImageEnhance
        import PIL.ImageOps

        img  = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        results = {}

        # Current price
        price_box = img.crop((int(w * 0.70), 0, w, int(h * 0.10)))
        price_box = price_box.resize((price_box.width * 2, price_box.height * 2), Image.LANCZOS)
        results["price_text"] = pytesseract.image_to_string(
            price_box, config="--psm 7 -c tessedit_char_whitelist=0123456789.-+"
        ).strip()

        # Signal box
        sig_box  = img.crop((0, int(h * 0.65), w, h))
        sig_box  = sig_box.resize((sig_box.width * 3, sig_box.height * 3), Image.LANCZOS)
        sig_box  = ImageEnhance.Contrast(sig_box).enhance(2.5)
        sig_box  = ImageEnhance.Sharpness(sig_box).enhance(2.0)
        sig_inv  = PIL.ImageOps.invert(sig_box.convert("L"))
        sig_bw   = sig_inv.point(lambda x: 255 if x > 100 else 0)
        results["signal_text"] = pytesseract.image_to_string(
            sig_bw,
            config=(
                "--psm 6 "
                "-c tessedit_char_whitelist="
                "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz./:- "
            )
        ).strip()

        return results

    except Exception as e:
        log.error(f"OCR error: {e}")
        return {}


# ── Signal parser ─────────────────────────────────────────────

def parse_all(ocr_results: dict) -> dict:
    sig  = {}
    raw  = ocr_results.get("signal_text", "")
    text = re.sub(r"[ \t]+", " ", raw).replace("\n", " ")

    m = re.search(r"(Long|Short)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["action"] = "BUY" if m.group(1).upper() == "LONG" else "SELL"
        sig["entry"]  = m.group(2)

    m = re.search(r"-?\s*Trail\s*SL\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["trail_sl"] = m.group(1)

    m = re.search(r"(?:Current\s+)?P\s*[/\\|l]?\s*L\s*[:\s]+([-\d.]+)\s*Points?", text, re.IGNORECASE)
    if m:
        sig["pnl"] = m.group(1)

    for i in range(1, 4):
        m = re.search(rf"Target\s*{i}\s*[:\s]+([\d.]+)", text, re.IGNORECASE)
        if m:
            sig[f"target{i}"] = m.group(1)
        ms = re.search(rf"Target\s*{i}[^T]*?(Achieved|Pending)", text, re.IGNORECASE)
        if ms:
            sig[f"t{i}_status"] = ms.group(1)

    m = re.search(r"(\d{4,6})", ocr_results.get("price_text", ""))
    if m:
        sig["current_price"] = m.group(1)

    log.info(f"Raw OCR signal text: {repr(text)}")
    return {k: v for k, v in sig.items() if v}


def detect_changes(old: dict, new: dict) -> list:
    changed = []
    for k in ["action", "entry", "trail_sl", "t1_status", "t2_status", "t3_status", "pnl"]:
        if old.get(k) != new.get(k):
            changed.append(k)
    return changed


# ── Message builder ───────────────────────────────────────────

def esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_message(sig: dict, changed: list, is_first: bool = False) -> str:
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    action = sig.get("action", "")
    emoji  = "🟢" if action == "BUY" else "🔴"

    lines = ["🛢 *MCX CRUDE OIL SIGNAL*", f"🕐 {esc(now)}"]

    if "current_price" in sig:
        lines.append(f"💹 *Current Price:* `{esc(adjust(sig['current_price']))}`")

    lines.append("─────────────────────")

    if action:
        direction = "Long" if action == "BUY" else "Short"
        lines.append(f"{emoji} *{action} SIGNAL*")
        entry_val = adjust(sig['entry']) if 'entry' in sig else '—'
        lines.append(f"📌 {direction} At   : `₹{esc(entry_val)}`")
    if "trail_sl" in sig:
        lines.append(f"🛑 Trail SL  : `{esc(adjust(sig['trail_sl']))}`")
    if "pnl" in sig:
        lines.append(f"📈 Current P/L : `{esc(sig['pnl'])} pts`")

    lines.append("─────────────────────")

    for i, (tkey, skey) in enumerate([
        ("target1","t1_status"),
        ("target2","t2_status"),
        ("target3","t3_status"),
    ], 1):
        if tkey in sig:
            status = sig.get(skey, "Pending")
            badge  = "✅" if status == "Achieved" else "🎯"
            lines.append(f"{badge} T{i}: `{esc(adjust(sig[tkey]))}` {esc(status)}")

    lines.append("─────────────────────")

    if is_first:
        lines.append("📡 *Monitoring live\\.\\.\\.*")
    elif changed:
        labels = []
