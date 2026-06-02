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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8852868919:AAGC69Nd3F3LyepIMW66Do-t_HAW-bhCPoQ")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8755501824")
POLL_INTERVAL    = 5     # seconds between checks
PRICE_OFFSET     = -2    # subtract 2 from all price values
# ══════════════════════════════════════════════════════════════

CHART_IMAGE_URL = "https://dow.autobuysellsignal.in/CRUDEOIL-I_Chart1.png"
STATE_FILE      = "crude_bot_state.json"
LOG_FILE        = "crude_bot.log"

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
    """Subtract PRICE_OFFSET from a numeric string, preserving decimal places."""
    try:
        val      = float(value_str)
        adjusted = val + PRICE_OFFSET
        decimals = len(value_str.split(".")[-1]) if "." in value_str else 0
        return f"{adjusted:.{decimals}f}"
    except Exception:
        return value_str


# ── OCR ───────────────────────────────────────────────────────

def ocr_image(image_bytes: bytes) -> str:
    """
    Extract ALL text from the signal box image.
    The image has light text on dark green background.
    We invert it so tesseract sees dark text on light background.
    """
    try:
        import io
        import pytesseract
        from PIL import Image, ImageEnhance, ImageOps, ImageFilter

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Scale up for better OCR accuracy
        scale = 3
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)

        # Invert: white text on dark green → dark text on light background
        img = ImageOps.invert(img)

        # Convert to grayscale and increase contrast
        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(3.0)

        # Threshold to pure black/white
        img = img.point(lambda x: 255 if x > 120 else 0)

        # Run tesseract in page-segmentation mode 6 (uniform block of text)
        text = pytesseract.image_to_string(
            img,
            config="--psm 6 --oem 3"
        ).strip()

        log.info(f"Raw OCR output:\n{text}")
        return text

    except Exception as e:
        log.error(f"OCR error: {e}")
        return ""


# ── Signal parser ─────────────────────────────────────────────

def parse_signal(raw_text: str) -> dict:
    """
    Parse the OCR text into structured signal data.
    Example input:
        Long At 8719 -Trail SL 8699.71
        Current P/L: 64 Points
        Target 1: 8762.595 :: Achieved
        Target 2: 8799.215
        Target 3: 8875.07
    """
    sig  = {}
    # Normalise whitespace
    text = re.sub(r"[ \t]+", " ", raw_text)

    # ── Direction + Entry
    # Matches: "Long At 8719" or "Short At 8720.5"
    m = re.search(r"(Long|Short)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["action"] = "BUY" if m.group(1).upper() == "LONG" else "SELL"
        sig["entry"]  = m.group(2)

    # ── Trail SL
    # Matches: "-Trail SL 8699.71" or "Trail SL 8699.71"
    m = re.search(r"-?\s*Trail\s+SL\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["trail_sl"] = m.group(1)

    # ── P&L — keep EXACT value as shown (no price offset applied)
    # Matches: "Current P/L: 64 Points" or "P/L: -12 Points" or "P/L 64 Points"
    m = re.search(r"P\s*/\s*L\s*[:\s]+([-\d.]+)\s*Points?", text, re.IGNORECASE)
    if m:
        sig["pnl"] = m.group(1)   # raw value, no adjustment

    # ── Targets 1-3
    for i in range(1, 4):
        # Value
        m = re.search(rf"Target\s*{i}\s*[:\s]+([\d.]+)", text, re.IGNORECASE)
        if m:
            sig[f"target{i}"] = m.group(1)
        # Status (Achieved / Pending)
        ms = re.search(rf"Target\s*{i}.{{0,30}}?(Achieved|Pending)", text, re.IGNORECASE)
        if ms:
            sig[f"t{i}_status"] = ms.group(1).capitalize()

    log.info(f"Parsed signal: {sig}")
    return sig


# ── State persistence ─────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def signal_changed(old: dict, new: dict) -> list:
    """Return list of keys that changed between old and new signal."""
    watch = ["action", "entry", "trail_sl", "pnl",
             "target1", "target2", "target3",
             "t1_status", "t2_status", "t3_status"]
    return [k for k in watch if old.get(k) != new.get(k)]


# ── Telegram ──────────────────────────────────────────────────

def esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_message(sig: dict, changed: list, is_first: bool = False) -> str:
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    action = sig.get("action", "—")
    emoji  = "🟢" if action == "BUY" else "🔴"

    lines = [
        "🛢 *MCX CRUDE OIL SIGNAL*",
        f"🕐 {esc(now)}",
        "─────────────────────",
    ]

    # Direction + Entry
    if action in ("BUY", "SELL"):
        direction = "Long" if action == "BUY" else "Short"
        entry_val = adjust(sig["entry"]) if "entry" in sig else "—"
        lines.append(f"{emoji} *{esc(action)} SIGNAL*")
        lines.append(f"📌 *Long At :* `{esc(entry_val)}`" if action == "BUY"
                     else f"📌 *Short At :* `{esc(entry_val)}`")

    # Trail SL
    if "trail_sl" in sig:
        lines.append(f"🛑 *Trail SL :* `{esc(adjust(sig['trail_sl']))}`")

    # P&L — exact value from image, no price offset
    if "pnl" in sig:
        pnl_val = sig["pnl"]
        pnl_num = float(pnl_val) if pnl_val.lstrip("-").replace(".", "").isdigit() else None
        pnl_emoji = "📈" if (pnl_num is not None and pnl_num >= 0) else "📉"
        lines.append(f"{pnl_emoji} *P&L :* `{esc(pnl_val)} pts`")

    lines.append("─────────────────────")

    # Targets
    target_map = [
        ("target1", "t1_status", "T1"),
        ("target2", "t2_status", "T2"),
        ("target3", "t3_status", "T3"),
    ]
    for tkey, skey, label in target_map:
        if tkey in sig:
            status = sig.get(skey, "Pending")
            badge  = "✅" if status == "Achieved" else "🎯"
            lines.append(f"{badge} *{label} :* `{esc(adjust(sig[tkey]))}` \\— {esc(status)}")

    lines.append("─────────────────────")

    if is_first:
        lines.append("📡 *Bot started \\— monitoring live\\.\\.\\.*")
    elif changed:
        nice = {
            "action":    "Direction",
            "entry":     "Entry price",
            "trail_sl":  "Trail SL",
            "pnl":       "P&L",
            "target1":   "Target 1",
            "target2":   "Target 2",
            "target3":   "Target 3",
            "t1_status": "T1 status",
            "t2_status": "T2 status",
            "t3_status": "T3 status",
        }
        updates = ", ".join(nice.get(k, k) for k in changed)
        lines.append(f"🔔 *Updated:* {esc(updates)}")

    return "\n".join(lines)


async def send_telegram(session: aiohttp.ClientSession, text: str, image_bytes: bytes = None):
    """Send a Telegram message, optionally with the chart image."""
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

    if image_bytes:
        # Send photo with caption
        url  = f"{base}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id",    TELEGRAM_CHAT_ID)
        data.add_field("caption",    text)
        data.add_field("parse_mode", "MarkdownV2")
        data.add_field("photo", image_bytes,
                       filename="chart.png", content_type="image/png")
        async with session.post(url, data=data) as r:
            resp = await r.json()
    else:
        url  = f"{base}/sendMessage"
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "MarkdownV2",
        }
        async with session.post(url, json=payload) as r:
            resp = await r.json()

    if not resp.get("ok"):
        log.error(f"Telegram error: {resp}")
        # Try plain text fallback (in case of MarkdownV2 parse errors)
        plain = re.sub(r"[\\*_`\[\]()]", "", text)
        payload2 = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    plain,
        }
        async with session.post(f"{base}/sendMessage", json=payload2) as r2:
            resp2 = await r2.json()
            if not resp2.get("ok"):
                log.error(f"Telegram plain-text fallback also failed: {resp2}")
            else:
                log.info("Sent via plain-text fallback.")
    else:
        log.info("Telegram message sent successfully.")


# ── Image fetcher ─────────────────────────────────────────────

async def fetch_image(session: aiohttp.ClientSession) -> bytes | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        async with session.get(
            CHART_IMAGE_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                return await r.read()
            log.warning(f"Image fetch returned HTTP {r.status}")
    except Exception as e:
        log.error(f"Image fetch error: {e}")
    return None


# ── Main loop ─────────────────────────────────────────────────

async def main():
    log.info("═══ Crude Oil Signal Bot starting ═══")

    state    = load_state()
    prev_sig = state.get("signal", {})
    is_first = not bool(prev_sig)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                img_bytes = await fetch_image(session)
                if img_bytes is None:
                    log.warning("No image received, skipping cycle.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Check image hash — skip OCR if image unchanged
                img_hash = hashlib.md5(img_bytes).hexdigest()
                if img_hash == state.get("img_hash") and not is_first:
                    log.info("Image unchanged (hash match), skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                raw_text = ocr_image(img_bytes)
                if not raw_text:
                    log.warning("OCR returned empty text, skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_sig = parse_signal(raw_text)
                if not new_sig:
                    log.warning("No signal data parsed, skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                changed = signal_changed(prev_sig, new_sig)

                if is_first or changed:
                    msg = build_message(new_sig, changed, is_first=is_first)
                    log.info(f"Sending Telegram message (changed: {changed or 'first run'})")
                    await send_telegram(session, msg, img_bytes)
                    prev_sig = new_sig
                    is_first = False
                    state = {"signal": new_sig, "img_hash": img_hash}
                    save_state(state)
                else:
                    log.info(f"No change detected. Signal: {new_sig}")

            except Exception as e:
                log.exception(f"Unexpected error in main loop: {e}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
