"""
signals.py  -  turns a day's prices into a short list of trade candidates.

Signals come from daily prices, so they only change once a day. The bot works
them out on the first run of the day and saves the short list. Every other run
just manages open positions and acts on that saved list.

With hundreds of symbols, more will signal than the bot can buy, so:
  1. Sells come first. They free up cash and reduce risk.
  2. Then buys, the most traded first: the same dollar volume that ranks the
     universe. Busy markets fill closest to the price you saw.
  3. Only the top MAX_AI_REVIEWS_PER_DAY candidates are kept. That is what caps
     the calls to Gemini.

Nothing here talks to Alpaca, Gemini or a file, so it can be read and tested alone.
The saved list is public, so it holds no prices or averages.
"""

import config
from strategy import BUY, SELL


def plain_reason(reason):
    """The strategy's reason without its price figures, which aren't for publishing."""
    return reason.split(" (")[0]


def scan(bars, held, signal_for):
    """The same buy and sell rules as always, applied to every symbol in bars."""
    found = []
    for symbol in sorted(bars):
        prices = bars[symbol]
        if len(prices) < config.SLOW_SMA_DAYS + 1:
            continue
        signal = signal_for(prices)
        holding = symbol in held
        action, note = None, ""
        if signal["signal"] == BUY:
            action = None if holding else BUY
        elif signal["signal"] == SELL:
            action = SELL if holding else None
        elif holding and signal["trend"] == "DOWN":
            action, note = SELL, "holding while the trend is down (sell signal was missed)"
        if action:
            reason = plain_reason(signal["reason"]) + (f"; {note}" if note else "")
            found.append({"symbol": symbol, "action": action, "reason": reason,
                          "trend": signal["trend"], "signal_date": signal["date"]})
    return found


def start_day(market, day, found, universe_rows, scanned):
    """Rank what was found and keep the top candidates for today."""
    ranks = {row["symbol"]: position for position, row in enumerate(universe_rows)}
    names = {row["symbol"]: row.get("name", "") for row in universe_rows}
    ordered = sorted(found, key=lambda c: (c["action"] != SELL, ranks.get(c["symbol"], len(ranks))))
    top = ordered[:config.MAX_AI_REVIEWS_PER_DAY[market]]
    for rank, candidate in enumerate(top, start=1):
        candidate.update(rank=rank, name=names.get(candidate["symbol"], ""), status="waiting",
                         ai_decision="", ai_reason="", note="")
    return {"day": day, "scanned": scanned, "found": len(found), "candidates": top}


def waiting(plan):
    """Candidates that still need something done today."""
    return [candidate for candidate in plan.get("candidates", []) if candidate["status"] == "waiting"]


def counts(plan):
    """Today's numbers for the daily summary."""
    candidates = plan.get("candidates", [])
    return {"found": plan.get("found", 0),
            "approved": sum(c["ai_decision"] == "APPROVE" for c in candidates),
            "vetoed": sum(c["ai_decision"] == "VETO" for c in candidates)}


def describe(plan, title):
    """Print the day's short list."""
    print(f"\n{title}: {plan.get('scanned', 0)} checked, {plan.get('found', 0)} candidates")
    for candidate in plan.get("candidates", []):
        print(f"  {candidate['rank']}. {candidate['symbol']:9} {candidate['action']:4} "
              f"{candidate['status']} · {candidate['reason']} [close of {candidate['signal_date']}]")
