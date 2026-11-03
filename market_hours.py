"""
market_hours.py  -  decides whether this workflow tick should run the bot at all.

The workflow ticks every 15 minutes, day and night, because each run starts the
next one. With only stock sleeves on, most of those ticks have nothing to do, so
they skip the bot and just keep the chain going.

The bot still runs on every tick if it was started by hand, or if crypto needs
watching: crypto never closes, so stops can't wait for the stock market to open.

This file needs no keys and no packages, and it can't place an order. If it fails,
the workflow runs the bot anyway, so a bug here can't switch off the stop-losses.
"""

import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NEW_YORK = ZoneInfo("America/New_York")
FIRST_TICK = (9, 5)      # New York time, so one tick lands before the 09:30 open and orders fill at it
LAST_TICK = (17, 0)      # an hour past the close, late enough for the 20:45 UTC daily summary
BOT_ACCOUNT = "github-actions[bot]"


def in_market_hours(now):
    """True on weekdays around US market hours. The bot itself checks for holidays."""
    local = now.astimezone(NEW_YORK)
    return local.weekday() < 5 and FIRST_TICK <= (local.hour, local.minute) < LAST_TICK


def crypto_is_trading(settings_text):
    """The old strategy trades crypto. In sleeves mode, only the crypto sleeves do."""
    if re.search(r"^CRYPTO_ENABLED = False", settings_text, re.M):
        return False
    if not re.search(r'^STRATEGY_MODE = "sleeves"', settings_text, re.M):
        return True
    return re.search(r"^ENABLE_CRYPTO_(TREND|HOLD) = True", settings_text, re.M) is not None


def holds_crypto(state_text):
    """A crypto position needs its stop checked even after its sleeve is switched off."""
    try:
        return any("/" in symbol for symbol in json.loads(state_text).get("positions", {}))
    except (ValueError, AttributeError):
        return True     # an unreadable file proves nothing, so stay on the safe side


def should_run(now, by_hand, settings_text, state_text):
    return by_hand or in_market_hours(now) or crypto_is_trading(settings_text) or holds_crypto(state_text)


def read(name, missing):
    try:
        with open(os.path.join(BASE_DIR, *name), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return missing


def main():
    by_hand = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
               and os.environ.get("GITHUB_TRIGGERING_ACTOR") != BOT_ACCOUNT)
    run = should_run(datetime.now(timezone.utc), by_hand, read(("config.py",), ""),
                     read(("state", "sleeves.json"), "{}"))
    print("Running the bot." if run else "Market hours are over and no crypto needs watching, so this tick skips the bot.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.write(f"run={'true' if run else 'false'}\n")


if __name__ == "__main__":
    main()
