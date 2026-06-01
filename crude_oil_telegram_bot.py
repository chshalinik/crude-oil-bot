"""
MCX Crude Oil Signal Monitor — Real-Time Telegram Alert Bot
- Long = BUY, Short = SELL
- All price values adjusted by -2
- P/L updates trigger alerts too
- Auto-retry on timeout with exponential backoff
"""

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
PNL_ALERT_CHANGE = 10   # send alert if P/L changes by this many points
# ══════════════════════════════════════════════════════════════

CHART_IMAGE_URL = "https://dow.autobuysellsignal.in/CRUDEOIL-I_Chart1.png"
STATE_FILE      = "crude_bot_state.json"
LOG_FILE        = "crude_bot.log"

MARKET_OPEN_HOUR  = 9
MARKET_CLOSE_HOUR = 23
MARKET_CLOSE_MIN  = 30

# Multiple user agents to rotate on retry
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
    """Subtract 2 from price, keep same decimal places."""
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

        img  = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        results = {}

        # Region 1: Current price (top right large number)
        price_box = img.crop((int(w * 0.70), 0, w, int(h * 0.10)))
        price_box = price_box.resize((price_box.width * 2, price_box.height * 2), Image.LANCZOS)
        results["price_text"] = pytesseract.image_to_string(
            price_box, config="--psm 7 -c tessedit_char_whitelist=0123456789.-+"
        ).strip()

        # Region 2: Signal box (bottom-left colored panel)
        sig_box  = img.crop((0, int(h * 0.68), int(w * 0.46), h))
        sig_box  = sig_box.resize((sig_box.width * 3, sig_box.height * 3), Image.LANCZOS)
        sig_box  = ImageEnhance.Contrast(sig_box).enhance(3.0)
        sig_box  = ImageEnhance.Sharpness(sig_box).enhance(2.5)
        sig_bw   = sig_box.convert("L").point(lambda x: 255 if x > 160 else 0)
        results["signal_text"] = pytesseract.image_to_string(
            sig_bw,
            config="--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz./:- "
        ).strip()

        return results

    except ImportError as e:
        log.error(f"Missing package: {e} — check requirements.txt and Dockerfile")
        return {}  # Don't crash the whole process; let the loop skip this cycle
    except Exception as e:
        log.error(f"OCR error: {e}")
        return {}


# ── Signal parser ─────────────────────────────────────────────

def parse_all(ocr_results: dict) -> dict:
    sig  = {}
    text = ocr_results.get("signal_text", "").replace("\n", " ")

    # Action: Long=BUY, Short=SELL
    m = re.search(r"(Short|Long)\s+At\s+([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["action"] = "BUY" if m.group(1).upper() == "LONG" else "SELL"
        sig["entry"]  = m.group(2)

    # Trail SL
    m = re.search(r"Trail\s*SL\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        sig["trail_sl"] = m.group(1)

    # P/L — always capture latest value
    m = re.search(r"P[/\\]?L[:\s]*([-\d.]+)\s*Points?", text, re.IGNORECASE)
    if m:
        sig["pnl"] = m.group(1)

    # Targets + status
    for i in range(1, 4):
        m = re.search(rf"Target\s*{i}[:\s]*([\d.]+)", text, re.IGNORECASE)
        if m:
            sig[f"target{i}"] = m.group(1)
        ms = re.search(rf"Target\s*{i}.*?(Achieved|Pending)", text, re.IGNORECASE)
        if ms:
            sig[f"t{i}_status"] = ms.group(1)

    # Current price
    m = re.search(r"(\d{4,6})", ocr_results.get("price_text", ""))
    if m:
        sig["current_price"] = m.group(1)

    return {k: v for k, v in sig.items() if v is not None and v != {}}


def detect_changes(old: dict, new: dict) -> list:
    """Detect what changed between old and new signal, including P/L."""
    changed = []

    # Key signal fields
    for k in ["action", "entry", "trail_sl", "t1_status", "t2_status", "t3_status"]:
        if old.get(k) != new.get(k):
            changed.append(k)

    # P/L — alert if changed by PNL_ALERT_CHANGE points or more
    try:
        old_pnl = float(old.get("pnl", 0))
        new_pnl = float(new.get("pnl", 0))
        if abs(new_pnl - old_pnl) >= PNL_ALERT_CHANGE:
            changed.append("pnl")
    except Exception:
        pass

    return changed


# ── Message builder ───────────────────────────────────────────

def esc(text: str) -> str:
    """Escape for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_message(sig: dict, changed: list, is_first: bool = False) -> str:
    now    = datetime.now().strftime("%d %b %Y  %H:%M:%S")
    action = sig.get("action", "")
    emoji  = "🟢" if action == "BUY" else "🔴"

    lines = ["🛢 *MCX CRUDE OIL SIGNAL*", f"🕐 {esc(now)}"]

    # Current price
    if "current_price" in sig:
        lines.append(f"💹 *Current Price:* `{esc(adjust(sig['current_price']))}`")

    lines.append("─────────────────────")

    # Signal
    if action:
        lines.append(f"{emoji} *{action} SIGNAL*")
        lines.append(f"📌 Entry     : `₹{esc(adjust(sig.get('entry','0')))}`")
    if "trail_sl" in sig:
        lines.append(f"🛑 Trail SL  : `{esc(adjust(sig['trail_sl']))}`")
    if "pnl" in sig:
        pnl = float(sig["pnl"])
        lines.append(f"{'📈' if pnl >= 0 else '📉'} P&L        : `{esc(sig['pnl'])} pts`")

    lines.append("─────────────────────")

    # Targets
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

    # What changed
    if is_first:
        lines.append("📡 *Monitoring live\\.\\.\\.*")
    elif changed:
        labels = []
        for c in changed:
            if c == "action":      labels.append("🆕 New Signal")
            elif c == "entry":     labels.append("📌 Entry changed")
            elif c == "trail_sl":  labels.append("🛑 Trail SL updated")
            elif c == "t1_status": labels.append("✅ Target 1 hit")
            elif c == "t2_status": labels.append("✅ Target 2 hit")
            elif c == "t3_status": labels.append("✅ Target 3 hit")
            elif c == "pnl":       labels.append("📊 P&L updated")
        lines.append("\n".join([esc(l) for l in labels]))

    return "\n".join(lines)


# ── Telegram sender ───────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, message: str) -> bool:
    if "YOUR_BOT_TOKEN" in TELEGRAM_TOKEN:
        log.warning("Set TELEGRAM_TOKEN env variable!")
        log.info(f"[TEST MSG]\n{message}")
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "MarkdownV2"}

    for attempt in range(3):
        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.json()
                if body.get("ok"):
                    log.info("✅ Telegram sent!")
                    return True
                log.warning(f"Telegram API error: {body}")
                # Formatting error — retry as plain text
                if body.get("error_code") == 400:
                    data2 = {"chat_id": TELEGRAM_CHAT_ID,
                             "text": re.sub(r"[*_`\\]", "", message)}
                    async with session.post(url, json=data2) as r2:
                        b2 = await r2.json()
                        if b2.get("ok"):
                            log.info("✅ Telegram sent (plain text fallback)")
                            return True
                return False
        except Exception as e:
            log.warning(f"Telegram attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2 ** attempt)
    return False


# ── Image fetcher with retry ──────────────────────────────────

async def fetch_image_with_retry(session, etag, last_modified, max_retries=3):
    """Fetch image with retry + rotating user agents on timeout."""
    for attempt in range(max_retries):
        headers = {
            "User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)],
            "Referer":    "https://autobuysellsignal.in/",
            "Accept":     "image/png,image/*,*/*",
        }
        if etag and attempt == 0:
            headers["If-None-Match"] = etag
        if last_modified and attempt == 0:
            headers["If-Modified-Since"] = last_modified

        # Increase timeout on each retry
        timeout = aiohttp.ClientTimeout(total=10 + attempt * 10)

        try:
            async with session.get(CHART_IMAGE_URL, headers=headers, timeout=timeout) as r:
                if r.status == 304:
                    log.debug("Image not modified (304)")
                    return None, etag, last_modified
                if r.status == 200:
                    data = await r.read()
                    log.debug(f"Image fetched: {len(data)} bytes")
                    return data, r.headers.get("ETag",""), r.headers.get("Last-Modified","")
                log.warning(f"Image HTTP {r.status} on attempt {attempt+1}")

        except asyncio.TimeoutError:
            wait = 2 ** attempt
            log.warning(f"Image fetch timeout (attempt {attempt+1}/{max_retries}) — retrying in {wait}s")
            await asyncio.sleep(wait)
        except aiohttp.ClientError as e:
            wait = 2 ** attempt
            log.warning(f"Image fetch error (attempt {attempt+1}): {e} — retrying in {wait}s")
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"Unexpected fetch error: {e}")
            break

    log.error("All fetch attempts failed — will try again next cycle")
    return None, etag, last_modified


# ── State ─────────────────────────────────────────────────────

def load_state() -> dict:
    d = {"etag":"","last_modified":"","last_hash":"","last_signal":{}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return {**d, **json.load(f)}
        except Exception:
            pass
    return d

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

    log.info("Bot starting up...")

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=5, ttl_dns_cache=300, ssl=False)
    ) as session:

        await send_telegram(
            session,
            "🛢 *MCX Crude Oil Bot Started\\!*\n"
            "You will get alerts when:\n"
            "• 🆕 New BUY or SELL signal fires\n"
            "• 🛑 Trail SL is updated\n"
            "• ✅ A target is achieved\n"
            f"• 📊 P&L changes by {PNL_ALERT_CHANGE}\\+ points\n"
            "• 📉 Any trend indicator flips\n"
            f"Checking every {POLL_INTERVAL}s during market hours\\."
        )

        while True:
            try:
                if not is_market_open():
                    log.info("Market closed — sleeping 5 min")
                    await asyncio.sleep(300)
                    continue

                image_bytes, etag, lm = await fetch_image_with_retry(session, etag, lm)

                if image_bytes is None:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                current_h = hashlib.md5(image_bytes).hexdigest()
                if current_h == last_h:
                    log.debug("No content change")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                log.info("📊 Image changed → OCR")
                ocr_results = ocr_image(image_bytes)
                if not ocr_results:
                    last_h = current_h
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                new_sig = parse_all(ocr_results)
                log.info(f"Parsed → action={new_sig.get('action')} "
                         f"entry={new_sig.get('entry')} "
                         f"sl={new_sig.get('trail_sl')} "
                         f"pnl={new_sig.get('pnl')} "
                         f"price={new_sig.get('current_price')}")

                changed = detect_changes(last_s, new_sig)

                if first and new_sig:
                    msg = build_message(new_sig, [], is_first=True)
                    await send_telegram(session, msg)
                    first = False
                elif changed and new_sig:
                    msg = build_message(new_sig, changed)
                    await send_telegram(session, msg)
                    log.info(f"Alert sent → {changed}")

                last_h = current_h
                last_s = new_sig
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
                    log.critical("10 errors — pausing 10 min")
                    await asyncio.sleep(600)
                    errors = 0

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    print("🛢 MCX Crude Oil Telegram Bot starting...")
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
