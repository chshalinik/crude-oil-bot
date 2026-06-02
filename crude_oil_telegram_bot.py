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
    try:
        val      = float(value_str)
        adjusted = val + PRICE_OFFSET
        decimals = len(value_str.split(".")[-1]) if "." in value_str else 0
        return f"{adjusted:.{decimals}f}"
    except Exception:
        return value_str


# ── OCR ───────────────────────────────────────────────────────

def ocr_image(image_bytes: bytes) -> str:
    try:
        import io
        import pytesseract
        from PIL import Image, ImageEnhance, ImageOps

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Scale up for better OCR accuracy
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)

        # Invert: white text on dark green → dark text on light background
        img = ImageOps.invert(img)

        # Grayscale + contrast boost
        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(3.0)

        # Binary threshold
        img = img.point(lambda x: 255 if x > 120 else 0)

        text = pytesseract.image_to_string(img, config="--psm 6 --oem 3").strip()
        log.info(f"Raw OCR output:\n{text}")
        return text

    except Exception as e:
        log.error(f"OCR error: {e}")
        return ""


# ── Signal parser ─────────────────────────────────────────────

def parse_signal(raw_text: str) -> dict:
    sig  = {}
    text = re.sub(r"[ \t]+", " ", raw_text)

    # Direction + Entry: "Long At 8719" / "Short At 8720.5"
    m = re.search(r"(Long|Short)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["action"] = "BUY" if m.group(1).upper() == "LONG" else "SELL"
        sig["entry"]  = m.group(2)

    # Trail SL: "-Trail SL 8699.71"
    m = re.search(r"-?\s*Trail\s+SL\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["trail_sl"] = m.group(1)

    # P&L — exact value, no offset: "Current P/L: 64 Points"
    m = re.search(r"P\s*/\s*L\s*[:\s]+([-\d.]+)\s*Points?", text, re.IGNORECASE)
    if m:
        sig["pnl"] = m.group(1)

    # Targets 1-3
    for i in range(1, 4):
        m = re.search(rf"Target\s*{i}\s*[:\s]+([\d.]+)", text, re.IGNORECASE)
        if m:
            sig[f"target{i}"] = m.group(1)
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
    watch = ["action", "entry", "trail_sl", "pnl",
             "target1", "target2", "target3",
             "t1_status", "t2_status", "t3_status"]
    return [k for k in watch if old.get(k) != new.get(k)]


# ── Telegram ──────────────────────────────────────────────────

def esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_message(sig: dict, changed: list) -> str:
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
        entry_val = adjust(sig["entry"]) if "entry" in sig else "—"
        label     = "Long At" if action == "BUY" else "Short At"
        lines.append(f"{emoji} *{esc(action)} SIGNAL*")
        lines.append(f"📌 *{esc(label)} :* `{esc(entry_val)}`")

    # Trail SL
    if "trail_sl" in sig:
        lines.append(f"🛑 *Trail SL :* `{esc(adjust(sig['trail_sl']))}`")

    # P&L — exact value from image, no price offset
    if "pnl" in sig:
        pnl_val = sig["pnl"]
        try:
            pnl_emoji = "📈" if float(pnl_val) >= 0 else "📉"
        except Exception:
            pnl_emoji = "📊"
        lines.append(f"{pnl_emoji} *P&L :* `{esc(pnl_val)} pts`")

    lines.append("─────────────────────")

    # Targets
    for i, (tkey, skey, label) in enumerate([
        ("target1", "t1_status", "T1"),
        ("target2", "t2_status", "T2"),
        ("target3", "t3_status", "T3"),
    ], 1):
        if tkey in sig:
            status = sig.get(skey, "Pending")
            badge  = "✅" if status == "Achieved" else "🎯"
            lines.append(f"{badge} *{label} :* `{esc(adjust(sig[tkey]))}` \\— {esc(status)}")

    lines.append("─────────────────────")

    # What changed
    if changed:
        nice = {
            "action":    "Direction",
            "entry":     "Entry",
            "trail_sl":  "Trail SL",
            "pnl":       "P&L",
            "target1":   "T1",
            "target2":   "T2",
            "target3":   "T3",
            "t1_status": "T1 status",
            "t2_status": "T2 status",
            "t3_status": "T3 status",
        }
        updates = ", ".join(nice.get(k, k) for k in changed)
        lines.append(f"🔔 *Updated:* {esc(updates)}")

    return "\n".join(lines)


async def send_telegram(session: aiohttp.ClientSession, text: str):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "MarkdownV2",
    }
    async with session.post(f"{base}/sendMessage", json=payload) as r:
        resp = await r.json()

    if not resp.get("ok"):
        log.error(f"Telegram MarkdownV2 error: {resp}")
        # Plain-text fallback
        plain = re.sub(r"[\\*_`\[\]()\-]", "", text)
        async with session.post(f"{base}/sendMessage",
                                json={"chat_id": TELEGRAM_CHAT_ID, "text": plain}) as r2:
            resp2 = await r2.json()
            if resp2.get("ok"):
                log.info("Sent via plain-text fallback.")
            else:
                log.error(f"Plain-text fallback also failed: {resp2}")
    else:
        log.info("Telegram message sent OK.")


# ── Image fetcher ─────────────────────────────────────────────

async def fetch_image(session: aiohttp.ClientSession) -> bytes | None:
    headers = {
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        "Cache-Control": "no-cache",
        "Pragma":        "no-cache",
    }
    try:
        async with session.get(
            CHART_IMAGE_URL, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                return await r.read()
            log.warning(f"Image fetch HTTP {r.status}")
    except Exception as e:
        log.error(f"Image fetch error: {e}")
    return None


# ── Main loop ─────────────────────────────────────────────────

async def main():
    log.info("═══ Crude Oil Signal Bot starting ═══")

    state    = load_state()
    prev_sig = state.get("signal", {})

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                img_bytes = await fetch_image(session)
                if img_bytes is None:
                    log.warning("No image, skipping cycle.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Skip OCR if image pixel-identical to last fetch
                img_hash = hashlib.md5(img_bytes).hexdigest()
                if img_hash == state.get("img_hash"):
                    log.info("Image unchanged, skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Image changed — always update hash so we don't re-OCR same image
                state["img_hash"] = img_hash

                raw_text = ocr_image(img_bytes)
                if not raw_text:
                    log.warning("OCR empty, skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_sig = parse_signal(raw_text)
                if not new_sig:
                    log.warning("Nothing parsed, skipping.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                changed = signal_changed(prev_sig, new_sig)

                if changed:
                    log.info(f"Change detected: {changed}")
                    msg = build_message(new_sig, changed)
                    await send_telegram(session, msg)
                    prev_sig       = new_sig
                    state["signal"] = new_sig
                    save_state(state)
                else:
                    log.info(f"No data change. Signal: {new_sig}")

            except Exception as e:
                log.exception(f"Loop error: {e}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
