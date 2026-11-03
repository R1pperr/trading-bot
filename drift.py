"""
drift.py  -  warns when a sleeve's live trades stop looking like its backtest.

Once a sleeve has 20 finished live trades, its latest 20 are compared with what any
run of 20 trades looked like in the out-of-sample test. If the average result per trade
or the win rate falls outside that range, you get one Telegram warning a day.

It only warns. It never changes a setting, a sleeve or a trade.
"""

import notifier

MIN_TRADES = 20


def compare(trades, ranges):
    """One row per sleeve with a tested range. status is "waiting", "ok" or "outside"."""
    rows = []
    for sleeve, limits in ranges.items():
        mine = [trade for trade in trades if trade.get("sleeve") == sleeve]
        row = {"sleeve": sleeve, "trades": len(mine), "status": "waiting", "expectancy": None, "win_rate": None,
               "limits": limits, "problems": []}
        if limits and len(mine) >= MIN_TRADES:
            latest = mine[-MIN_TRADES:]
            row["expectancy"] = sum(trade["ret"] for trade in latest) / len(latest)
            row["win_rate"] = sum(trade["pnl"] > 0 for trade in latest) / len(latest)
            if not limits["expectancy_low"] <= row["expectancy"] <= limits["expectancy_high"]:
                row["problems"].append(
                    f"average per trade {row['expectancy']:+.2%}, tested range "
                    f"{limits['expectancy_low']:+.2%} to {limits['expectancy_high']:+.2%}")
            if not limits["win_low"] <= row["win_rate"] <= limits["win_high"]:
                row["problems"].append(
                    f"win rate {row['win_rate']:.0%}, tested range {limits['win_low']:.0%} to {limits['win_high']:.0%}")
            row["status"] = "outside" if row["problems"] else "ok"
        rows.append(row)
    return rows


def warn(rows, warned, today):
    """Send one message a day per sleeve that's outside its range. Returns the updated record."""
    warned = dict(warned or {})
    for row in rows:
        if row["status"] == "outside" and warned.get(row["sleeve"]) != today:
            text = (f"⚠️ Drift warning: {row['sleeve']}\nIts last {MIN_TRADES} live trades look different from the "
                    f"backtest: {'; '.join(row['problems'])}.\nThis is only a warning. Nothing was changed.")
            if notifier.send(text):
                warned[row["sleeve"]] = today
    return warned
