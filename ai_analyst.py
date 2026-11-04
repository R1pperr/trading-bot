"""
ai_analyst.py  -  a second opinion from Claude (paid, first choice) or Google's Gemini (free).

Set USE_AI_ANALYST in config.py to turn it on or off. Keys go in .env or GitHub Secrets:
ANTHROPIC_API_KEY for Claude, GEMINI_API_KEY for Gemini. Claude is asked first while this
month's spend is under config.CLAUDE_MONTHLY_BUDGET_USD (counted in state/claude_spend.json);
without a key, over the budget, or if Claude fails, the free Gemini models answer.

The AI mode decides what its answer does (mode() below: docs/data/control.json, which the
daily review may change, else config.AI_VETO_MODE). In "shadow" it only logs its decision
and reason, and the trade goes ahead either way, so you can see how the trades it would
have vetoed actually did. In "enforce" a VETO blocks the trade.

Its prompt includes a short summary of its own recent record (how its last 50 calls turned
out), written by the daily review. AI failures in a row (Claude and Gemini both failing) are
counted in state/ai_health.json, and the watchdog alerts at 3.

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

Check the keys work:   python ai_analyst.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

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
Don't quote prices or other numbers in your reason; describe the move in words.
Reply with JSON only: {"decision": "APPROVE" or "VETO", "reason": "<one sentence, at most 25 words>"}"""


HEALTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "ai_health.json")
CLAUDE_SPEND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "claude_spend.json")
DECISION_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["decision", "reason"],
    "additionalProperties": False,
}


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


BUSY_CODES = (429, 500, 503, 504)
# Only for the AI check (python ai_analyst.py): other free models to try when both configured ones fail.
PROBE_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash",
                "gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest")
RETRY_WAITS = (3, 10)   # seconds before the second and third try


def is_busy(error):
    """True for Gemini saying it's overloaded or rate-limited, which usually passes in seconds."""
    code = getattr(error, "code", None)
    return code in BUSY_CODES or isinstance(error, TimeoutError) or "UNAVAILABLE" in str(error)


def models_to_try():
    """Three tries: the main model, then the free backups in order (repeating the last if short)."""
    tries = [config.GEMINI_MODEL] + [m for m in getattr(config, "GEMINI_BACKUP_MODELS", ()) if m]
    while len(tries) < 3:
        tries.append(tries[-1])
    return tries[:3]


def with_retries(send, models=None, sleep=time.sleep):
    """
    Gemini's free tier sometimes answers 503 "high demand": on 4 Nov 2026 gemini-3.8-flash
    did so for hours. send(model) is tried with the main model, then after 3 and 10 seconds
    with the backup models. Any other error (a bad key, a bad model name) fails straight away.
    Returns (send's result, the model that answered).
    """
    models = models or models_to_try()
    for attempt, model in enumerate(models):
        if attempt:
            sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS)) - 1])
        try:
            return send(model), model
        except Exception as error:
            if attempt == len(models) - 1 or not is_busy(error):
                raise


def generate(prompt, generation_config=None, models=None):
    """
    One Gemini question with retries and the backup model. Returns (response, model).
    The client is kept in a variable for the whole call: a client that is only used inline
    can be closed before its request goes out ("the client has been closed", 4 Nov 2026).
    """
    from google import genai

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")
    client = genai.Client(api_key=api_key)
    return with_retries(lambda model: client.models.generate_content(
        model=model, contents=prompt, config=generation_config), models)


# ---------------------------------------------------------------------------
# Claude (paid, asked first)
# ---------------------------------------------------------------------------
class ClaudeSkipped(Exception):
    """Claude isn't asked: there's no key, or this month's budget is used up."""


def this_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def claude_spend():
    """Dollars spent on Claude this month, from the token counts of every answer."""
    try:
        with open(CLAUDE_SPEND_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return float(data.get("usd", 0.0)) if data.get("month") == this_month() else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


def _add_claude_spend(usage):
    price = config.CLAUDE_PRICE_PER_MILLION
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    cost = (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000
    try:
        with open(CLAUDE_SPEND_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("month") != this_month():
            data = {}
    except (OSError, ValueError, TypeError):
        data = {}
    data = {"month": this_month(), "usd": round(float(data.get("usd", 0.0)) + cost, 6),
            "calls": int(data.get("calls", 0)) + 1}
    try:
        os.makedirs(os.path.dirname(CLAUDE_SPEND_FILE), exist_ok=True)
        with open(CLAUDE_SPEND_FILE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.write("\n")
    except OSError:
        pass
    return cost


def ask_claude(prompt, json_answer=True, max_tokens=4000):
    """
    One question to Claude. Returns (text, model). Raises ClaudeSkipped when it isn't asked,
    and any other error when the call fails. The SDK itself retries busy answers (429, 5xx).
    """
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ClaudeSkipped("no ANTHROPIC_API_KEY is set")
    if claude_spend() >= config.CLAUDE_MONTHLY_BUDGET_USD:
        raise ClaudeSkipped(f"this month's Claude budget (${config.CLAUDE_MONTHLY_BUDGET_USD:.2f}) is used up")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=60)
    output_config = {"effort": config.CLAUDE_EFFORT}
    if json_answer:
        output_config["format"] = {"type": "json_schema", "schema": DECISION_SCHEMA}
    response = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",          # a safety decline is retried on Anthropic's recommended model
        output_config=output_config,
        messages=[{"role": "user", "content": prompt}],
    )
    _add_claude_spend(response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.model


def _parse_decision(text):
    answer = json.loads(text)
    decision = str(answer.get("decision", "")).strip().upper()
    reason = mask_prices(" ".join(str(answer.get("reason", "")).split())[:200]) or "(no reason given)"
    if decision not in (APPROVE, VETO):
        return VETO, f"{UNCLEAR_PREFIX} ({decision!r}){_outcome()}"
    return decision, reason


def _ask_ai(prompt):
    """Claude first; the free Gemini models if Claude has no key, is over budget, or fails."""
    try:
        text, _ = ask_claude(prompt)
        return _parse_decision(text)
    except ClaudeSkipped:
        pass
    except Exception as error:
        print("   Claude failed, so Gemini was asked:", " ".join(str(error).split())[:150])
    return _ask_gemini(prompt)


def write_text(prompt):
    """A plain-text answer (the daily review's explanation): Claude first, then Gemini."""
    try:
        text, _ = ask_claude(prompt, json_answer=False)
        return text
    except ClaudeSkipped:
        pass
    except Exception as error:
        print("   Claude failed, so Gemini was asked:", " ".join(str(error).split())[:150])
    from google.genai import types
    response, _ = generate(prompt, types.GenerateContentConfig(temperature=0))
    return response.text or ""


# ---------------------------------------------------------------------------
# Gemini (free, the backup)
# ---------------------------------------------------------------------------
def _ask_gemini(prompt, models=None):
    """Send the prompt to Gemini and return (decision, reason). Raises on any problem."""
    from google.genai import types

    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "decision": types.Schema(type=types.Type.STRING),
            "reason": types.Schema(type=types.Type.STRING),
        },
        required=["decision", "reason"],
    )
    response, model = generate(prompt, types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0,
    ), models)
    first = (models or models_to_try())[0]
    if model != first:
        print(f"   {first} was busy, so the backup {model} answered")
    return _parse_decision(response.text)


def mask_prices(text):
    """
    Blanks out prices: the reason ends up in public files (the activity feed, the trade log), and
    Alpaca's prices can't be passed on. On 4 Nov 2026 a veto quoted two closing prices.
    The same rule as the real-time engine's maskPrices.
    """
    text = re.sub(r"\$\s?\d[\d,]*(?:\.\d+)?", "…", str(text))
    text = re.sub(r"\d+\.\d+", "…", text)
    return re.sub(r"\b\d{3,}\b", "…", text)


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
    keys = [os.getenv(name, "").strip() for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")]
    if mode() != "enforce" and not any(keys):
        return OFF, "no AI key is set, so the AI wasn't asked"
    try:
        answer = _ask_ai(_build_prompt(symbol, signal, reason, recent_prices.tail(30), rule, record_summary()))
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

    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=30)
    example = pd.DataFrame({"close": [100 + i * 0.4 for i in range(30)]}, index=days)
    example_prompt = _build_prompt("SPY", "BUY", "20-day average crossed ABOVE the 50-day average (example)", example)
    answered = []
    print(f"Asking {config.CLAUDE_MODEL} (Claude) about a made-up example trade (nothing is traded)...")
    try:
        text, served_by = ask_claude(example_prompt)
        decision, why = _parse_decision(text)
        print(f"✅ Claude ({served_by}) answered: {decision} - {why}")
        answered.append(served_by)
    except ClaudeSkipped as skipped:
        print(f"⏭️  Claude not asked: {skipped}")
    except Exception as error:
        print("❌ Claude failed:", " ".join(str(error).split())[:300])
    print(f"   Claude spend this month: ${claude_spend():.4f} of ${config.CLAUDE_MONTHLY_BUDGET_USD:.2f}")
    for name in dict.fromkeys(models_to_try()):     # the main model, then the backup, each checked on its own
        print(f"Asking {name} about a made-up example trade (nothing is traded)...")
        try:
            decision, why = _ask_gemini(example_prompt, models=[name, name])
            print(f"✅ {name} answered: {decision} - {why}")
            answered.append(name)
        except Exception as error:
            print(f"❌ {name} failed:", " ".join(str(error).split())[:300])
    if not answered:
        print("   Trying other free models once each, to find a backup that answers today:")
        for name in PROBE_MODELS:
            try:
                decision, why = _ask_gemini(example_prompt, models=[name])
                print(f"   ✅ {name} answered: {decision}")
            except Exception as error:
                print(f"   ❌ {name}:", " ".join(str(error).split())[:90])
        print("   Check GEMINI_API_KEY in .env, and GEMINI_MODEL / GEMINI_BACKUP_MODELS in config.py")
        print("   against the free models listed in Google AI Studio.")
        try:                                  # names only, to pick a model that exists
            from google import genai
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "").strip())
            names = sorted(m.name.replace("models/", "") for m in client.models.list()
                           if "flash" in m.name and "generateContent" in (getattr(m, "supported_actions", None) or []))
            print("   Flash models this key can use:", ", ".join(names) or "none found")
        except Exception as listing:
            print("   Could not list the models:", " ".join(str(listing).split())[:200])
        sys.exit(1)
