"""
watchdog.py  -  the main bot keeps an eye on the real-time engine, and on itself.

Each main-bot run (every 15 minutes) checks:
  * the real-time engine's heartbeat on its /status page: during market hours it should have run
    within the last 5 minutes;
  * a position left without a working stop;
  * 3 Gemini failures in a row.
Each problem sends one Telegram alert, then at most one an hour while it lasts.

The engine checks the main bot the other way round (realtime/src). Errors and the kill switch
already alert through notifier.py. Reads only: nothing here can place or change an order.

Test it without waiting for a real problem:  python bot.py --watchdog-test
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import ai_analyst
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state", "watchdog.json")
SITE_FILE = os.path.join(BASE_DIR, "docs", "data", "site.json")
ENGINE_LATE_MINUTES = 5
REPEAT = timedelta(hours=1)
LABEL = "[WATCHDOG]"


def engine_url():
    try:
        with open(SITE_FILE, encoding="utf-8") as handle:
            return str(json.load(handle).get("worker_url", "")).rstrip("/")
    except (OSError, ValueError):
        return ""


def fetch_engine_status(url=None, timeout=10):
    url = url or engine_url()
    if not url:
        return None
    request = urllib.request.Request(f"{url}/status", headers={"user-agent": "tach-trading-bot-watchdog"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def engine_problem(status, now, market_open):
    """A plain-English problem with the real-time engine, or "" if it looks fine."""
    if not market_open:
        return ""
    if status is None:
        return "the real-time engine's status page didn't answer"
    beat = (status.get("heartbeat") or {}).get("last_run_ms")
    if not beat:
        return "the real-time engine has no heartbeat yet"
    minutes = (now - datetime.fromtimestamp(beat / 1000, timezone.utc)).total_seconds() / 60
    if minutes > ENGINE_LATE_MINUTES:
        return f"the real-time engine hasn't run for {minutes:.0f} minutes"
    return ""


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
        handle.write("\n")


def due(state, kind, now):
    last = state.get(kind)
    return not last or now - datetime.fromisoformat(last) >= REPEAT


def problems(now, market_open, engine_status, unprotected, gemini_failures):
    found = {}
    engine = "" if engine_status == "skip" else engine_problem(engine_status, now, market_open)
    if engine:
        found["engine_late"] = engine
    if unprotected and market_open:
        found["no_stop"] = f"no working stop order for {', '.join(unprotected)}"
    if gemini_failures >= 3:
        found["gemini"] = f"Gemini failed {gemini_failures} times in a row, so buys are blocked (enforce mode)"
    return found


def check(now, market_open, unprotected=(), engine_status="fetch", send=notifier.send, test=False):
    """Run every check and send what's new. Returns the problems found. Never raises."""
    try:
        if engine_status == "fetch":
            url = engine_url()
            if not url:
                engine_status = "skip"          # no engine set up, so nothing to check
            else:
                try:
                    engine_status = fetch_engine_status(url)
                except Exception:
                    engine_status = None
        found = problems(now, market_open, engine_status, list(unprotected), ai_analyst.failures_in_row())
        state = load_state()
        for kind, text in found.items():
            if test or due(state, kind, now):
                if send(f"{LABEL}{' [TEST]' if test else ''} ⚠️ {text}"):
                    if not test:
                        state[kind] = now.isoformat()
        for kind in [k for k in state if k not in found]:
            del state[kind]                     # cleared, so the next problem alerts straight away
        if not test:
            save_state(state)
        return found
    except Exception as error:
        print("⚠️  Watchdog check failed:", notifier.redact(error))
        return {}


def simulate_missed_run(now, send=notifier.send):
    """The test the brief asks for: pretend the engine's last run was 2 hours ago. Places nothing."""
    fake = {"heartbeat": {"last_run_ms": int((now - timedelta(hours=2)).timestamp() * 1000)}}
    return check(now, True, (), fake, send, test=True)
