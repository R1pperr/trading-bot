"""
notifier.py  -  optional phone alerts through a Telegram bot.

OFF until TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set (as GitHub Secrets, or
in .env on your own computer). If either is missing, nothing is sent.

bot.py calls notify() at the very end of a run, after every order has gone
through, so a slow or broken Telegram cannot delay or block a trade. Nothing in
this file raises an error back to the bot: a failed message is printed and the
run carries on.

Check your Telegram setup:   python notifier.py
"""

import os
import sys
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import ai_analyst

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 10
MAX_MESSAGE_LENGTH = 4000   # Telegram allows 4096 characters
SECRET_NAMES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN")
BOTH_LABEL = "[STOCK] [CRYPTO]"


def redact(text):
    """Blank out any key, so it can't reach a message, a log or a public file."""
    text = str(text)
    for name in SECRET_NAMES:
        secret = os.getenv(name, "").strip()
        if secret:
            text = text.replace(secret, "***")
    return text


def send(message):
    """Send one message. Returns True if Telegram took it. Never raises an error."""
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return False
        fields = {"chat_id": chat_id, "text": redact(message)[:MAX_MESSAGE_LENGTH]}
        request = urllib.request.Request(SEND_URL.format(token=token),
                                         data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            return True
    except Exception as error:
        print("⚠️  Telegram message not sent:", redact(error))
        return False


def is_crypto_row(row):
    return "/" in row.get("symbol", "") or row.get("details", "").startswith("crypto:")


def label_for(row):
    """Every message says which side it's about. The kill switch covers both."""
    if row.get("event") == "KILL_SWITCH":
        return BOTH_LABEL
    return "[CRYPTO]" if is_crypto_row(row) else "[STOCK]"


def _order_message(row):
    qty = f"{float(row['qty']):.8f}".rstrip("0").rstrip(".") if is_crypto_row(row) else f"{float(row['qty']):g}"
    price = f"${float(row['price']):,.2f}"
    status = row.get("order_status", "")
    outcome = "filled" if status == "filled" else f"placed ({status})"
    details = row.get("details", "")

    decision = row.get("ai_decision", "")
    if decision and row.get("ai_reason") and decision != ai_analyst.OFF:
        ai_line = f"AI: {decision} · {row['ai_reason']}"
    else:
        ai_line = f"AI: {decision or 'not asked'}"

    if details.startswith("stop-loss"):
        title = f"🛑 STOP-LOSS: SELL {qty} {row['symbol']} at {price}, {outcome}"
    else:
        icon = "🟢" if row.get("side") == "buy" else "🔴"
        title = f"{icon} {row['side'].upper()} {qty} {row['symbol']} at {price}, {outcome}"
    return f"{title}\nReason: {details}\n{ai_line}"


def message_for(row):
    """Turn one trade-log row into a labeled phone message, or None if it isn't worth one."""
    message = _message_text(row)
    return f"{label_for(row)} {message}" if message else None


def _message_text(row):
    event = row.get("event")
    if event == "ORDER":
        return _order_message(row)
    if event == "KILL_SWITCH":
        return f"⛔ KILL SWITCH ON\n{row.get('details', '')}"
    if event == "ERROR":     # AI vetoes are counted in the daily summary instead
        return f"❌ Bot run crashed\n{row.get('details', '')}"
    return None


def notify(rows):
    """Send a message for each trade-log row that needs one. Never raises an error."""
    for row in rows:
        try:
            message = message_for(row)
        except Exception as error:
            print("⚠️  Telegram message not built:", redact(error))
            continue
        if message:
            send(message)


if __name__ == "__main__":
    load_dotenv()
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or not os.getenv("TELEGRAM_CHAT_ID", "").strip():
        print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing, so nothing was sent.")
        sys.exit(1)
    if send("✅ Test message from your trading bot. Telegram alerts work. No order was placed."):
        print("✅ Telegram accepted the test message. Check your phone.")
    else:
        print("❌ Telegram refused the test message. Check the token and the chat ID.")
        sys.exit(1)
