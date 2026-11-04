"""
activity.py  -  the main bot's live activity feed, in plain English.

Every run adds a few lines about what it checked and decided, for example
"IWM: IBS dip → buy candidate → AI approved (calm market) → order placed". The dashboard's
Live tab shows them, newest first.

The feed is a public file, so it never holds market prices or indicator values (Alpaca's
data can't be passed on). It only holds the rule that fired and our own fill prices.
"""

import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEED_FILE = os.path.join(BASE_DIR, "docs", "data", "activity.json")
KEEP = 300

_lines = []


def say(text, when=None):
    """Remember one line for this run's feed."""
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _lines.append({"t": stamp, "bot": "main", "text": " ".join(str(text).split())[:240]})


def pending():
    return list(_lines)


def clear():
    _lines.clear()


def flush(path=None):
    """Add this run's lines to the top of the saved feed. Keeps the newest 300."""
    if not _lines:
        return
    path = path or FEED_FILE
    try:
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, list):
            saved = []
    except (OSError, ValueError):
        saved = []
    feed = list(reversed(_lines)) + saved
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(feed[:KEEP], handle, separators=(",", ":"))
        handle.write("\n")
    os.replace(path + ".tmp", path)
    clear()
