"""
ai_analyst.py  -  optional second opinion from Google's Gemini (free tier).

Set USE_AI_ANALYST in config.py to turn it on or off, and put your key in .env as
GEMINI_API_KEY=... (or in GitHub Secrets for the scheduled runs).

The AI mode decides what its answer does (mode() below: docs/data/control.json, which the
daily review may change, else config.AI_VETO_MODE). In "shadow" it only logs its decision
and reason, and the trade goes ahead either way, so you can see how the trades it would
have vetoed actually did. In "enforce" a VETO blocks the trade.

Its prompt includes a short summary of its own recent record (how its last 50 calls turned
out), written by the daily review. Gemini failures in a row are counted in
state/ai_health.json, and the watchdog alerts at 3.

Before a strategy trade, the AI sees the signal and the last 30 closing prices
and answers APPROVE or VETO with a one-line reason. That is all it can do:
  * it never sees or decides the position size (risk.py does that)
  * it is never asked about stop-loss sells (those always go through)
  * if anything goes wrong (no key, no internet, a strange answer, free-tier
    limit reached) the answer is VETO, so in "enforce" the bot never trades on an
    AI error. Those answers don't count against the AI in the shadow scoreboard.
    In "shadow" a missing key just means the AI isn't asked (OFF).

The AI only sees prices, not news, and has no proven ability to pick trades.
Treat it as a learning experiment, not an edge.

Check your Gemini key works:   python ai_analyst.py
"""

import json
import os
import sys

from dotenv import load_dotenv

import config

APPROVE = "APPROVE"
VETO = "VETO"
OFF = "OFF"
ERROR_PREFIX = "AI error"
UNCLEAR_PREFIX = "AI gave an unclear answer"

INSTRUCTIONS = """You are a cautious reviewer for a student's paper-trading (simulated money) project.
A simple rule-based strategy wants to make one trade. Decide only whether to APPROVE or VETO it.
VETO only if the recent prices give a clear reason the signal looks unreliable, for example:
an extreme one-day move likely to reverse, or prices so choppy there is no real trend.
Otherwise APPROVE. You cannot change the trade size, and you must not suggest one.
Reply with JSON only: {"decision": "APPROVE" or "VETO", "reason": "<one sentence, at most 25 words>"}"""


HEALTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "ai_health.json")


def mode():
    """'enforce' or 'shadow'. The daily review can switch it in control.json; config is the default."""
    import fixes
    return fixes.load_control()["ai_mode"]["main"]


def record_summary():
    import fixes
    return fixes.load_control()["ai_record"]["main"]


def _build_prompt(symbol, signal, reason, recent_prices, rule=None, record=""):
    lines = [f"{day.date().isoformat()}: {close:.2f}" for day, close in recent_prices["close"].items()]
    rule = rule or f"{config.FAST_SMA_DAYS}/{config.SLOW_SMA_DAYS}-day moving-average crossover"
    history = f"\nYour own recent record on this bot (learn from it): {record}\n" if record else ""
    return (
        f"{INSTRUCTIONS}\n{history}\n"
        f"Proposed trade: {signal} {symbol}\n"
        f"Strategy: {rule}\n"
        f"Why the strategy wants it: {reason}\n"
        f"Last {len(lines)} daily closing prices, oldest first:\n" + "\n".join(lines)
    )


def failures_in_row():
    try:
        with open(HEALTH_FILE, encoding="utf-8") as handle:
            return int(json.load(handle).get("failures_in_row", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _note_health(failed):
    """Count Gemini failures in a row. A good answer resets it."""
    from datetime import datetime, timezone
    count = failures_in_row() + 1 if failed else 0
    try:
        os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
        with open(HEALTH_FILE, "w", encoding="utf-8") as handle:
            json.dump({"failures_in_row": count,
                       "last_error_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if failed else ""},
                      handle)
            handle.write("\n")
    except OSError:
        pass


def _ask_gemini(prompt):
    """Send the prompt to Gemini and return (decision, reason). Raises on any problem."""
    from google import genai
    from google.genai import types

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")

    client = genai.Client(api_key=api_key)
    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "decision": types.Schema(type=types.Type.STRING),
            "reason": types.Schema(type=types.Type.STRING),
        },
        required=["decision", "reason"],
    )
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
        ),
    )
    answer = json.loads(response.text)
    decision = str(answer.get("decision", "")).strip().upper()
    reason = " ".join(str(answer.get("reason", "")).split())[:200] or "(no reason given)"
    if decision not in (APPROVE, VETO):
        return VETO, f"{UNCLEAR_PREFIX} ({decision!r}){_outcome()}"
    return decision, reason


def is_real_veto(decision, reason):
    """True for a veto the AI chose, not one forced by an error or a strange answer."""
    return decision == VETO and not str(reason).startswith((ERROR_PREFIX, UNCLEAR_PREFIX))


def review(symbol, signal, reason, recent_prices, rule=None):
    """
    Ask the AI about one proposed trade.
    recent_prices: a table with a 'close' column and dates as the index.
    rule: one line on what the strategy does. The old crossover is the default.
    Returns (decision, one_line_reason) where decision is APPROVE, VETO or OFF.
    """
    if not config.USE_AI_ANALYST:
        return OFF, "AI analyst is turned off in config.py"
    load_dotenv()
    if mode() != "enforce" and not os.getenv("GEMINI_API_KEY", "").strip():
        return OFF, "no Gemini key is set, so the AI wasn't asked"
    try:
        answer = _ask_gemini(_build_prompt(symbol, signal, reason, recent_prices.tail(30), rule, record_summary()))
    except Exception as error:
        _note_health(True)
        short = " ".join(str(error).split())[:150]
        return VETO, f"{ERROR_PREFIX}{_outcome()}: {short}"
    _note_health(answer[1].startswith(UNCLEAR_PREFIX))
    return answer


def _outcome():
    """Only "enforce" stops the trade, so only then does the reason say so."""
    return ", so no trade" if mode() == "enforce" else ", trade not blocked"


if __name__ == "__main__":
    import pandas as pd

    print(f"Asking {config.GEMINI_MODEL} about a made-up example trade (nothing is traded)...")
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=30)
    example = pd.DataFrame({"close": [100 + i * 0.4 for i in range(30)]}, index=days)
    try:
        decision, why = _ask_gemini(_build_prompt(
            "SPY", "BUY", "20-day average crossed ABOVE the 50-day average (example)", example))
        print(f"✅ Gemini answered: {decision} - {why}")
    except Exception as error:
        print("❌ Gemini call failed:", " ".join(str(error).split())[:300])
        print("   Check GEMINI_API_KEY in .env, and GEMINI_MODEL in config.py against the")
        print("   free models listed in Google AI Studio.")
        try:                                  # names only, to pick a model that exists
            from google import genai
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "").strip())
            names = sorted(m.name.replace("models/", "") for m in client.models.list()
                           if "flash" in m.name and "generateContent" in (getattr(m, "supported_actions", None) or []))
            print("   Flash models this key can use:", ", ".join(names) or "none found")
        except Exception as listing:
            print("   Could not list the models:", " ".join(str(listing).split())[:200])
        sys.exit(1)
