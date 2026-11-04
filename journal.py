"""
journal.py  -  turns Alpaca's filled orders into a list of finished trades.

A finished trade is one buy matched with the sell that closed it, at the real fill
prices, tagged with the sleeve from the order's id. This includes stops that filled at
Alpaca, which the bot never saw happen. The drift monitor and the dashboard's trade
numbers read this list. It is plain data with no Alpaca calls, so tests can feed it
hand-made orders.
"""

from datetime import date, datetime, timedelta, timezone

import config
import crypto

MAX_TRADES = 500        # newest finished trades kept for the dashboard
MAX_SEEN = 1500         # newest order ids remembered, so an order is never counted twice
START_DAY = "2026-11-02"


def sleeve_of(order):
    """Order ids look like tach-rsi2_stocks-buy-abc123. Anything else has no sleeve."""
    parts = str(getattr(order, "client_order_id", "") or "").split("-")
    return parts[1] if len(parts) >= 4 and parts[0] == "tach" and parts[1] in config.SLEEVES else ""


def empty():
    return {"lots": {}, "seen": [], "trades": []}


def days_between(first, last):
    return max(0, (date.fromisoformat(last) - date.fromisoformat(first)).days)


def add_fill(book, order):
    """One filled order. A buy adds to the open lot, a sell closes it and writes a finished trade."""
    symbol, side = crypto.normalize(order.symbol) if "/" in order.symbol else order.symbol, crypto.text(order.side)
    qty, price = float(order.filled_qty), float(order.filled_avg_price)
    day = str(order.filled_at)[:10]
    lots = book["lots"]
    if side == "buy":
        lot = lots.setdefault(symbol, {"qty": 0.0, "cost": 0.0, "sleeve": sleeve_of(order), "day": day,
                                       "order_id": str(order.id), "time": str(order.filled_at)[:19]})
        lot["qty"] += qty
        lot["cost"] += qty * price
        return
    lot = lots.get(symbol)
    if lot is None or lot["qty"] <= 0:
        return                                  # bought before the journal began, so its cost isn't known
    sold = min(qty, lot["qty"])
    entry = lot["cost"] / lot["qty"]
    book["trades"].append({"symbol": symbol, "sleeve": lot["sleeve"] or sleeve_of(order), "entry_day": lot["day"],
                           "exit_day": day, "days": days_between(lot["day"], day), "qty": round(sold, 6),
                           "entry": round(entry, 4), "exit": round(price, 4), "pnl": round((price - entry) * sold, 2),
                           "ret": round(price / entry - 1, 5), "stop": "-stop-" in str(order.client_order_id or ""),
                           "entry_order": lot["order_id"], "entry_time": lot.get("time", ""),
                           "exit_time": str(order.filled_at)[:19], "exit_order": str(order.id)})
    lot["qty"] -= sold
    lot["cost"] -= sold * entry
    if lot["qty"] <= 1e-9:
        del lots[symbol]


def update(book, orders):
    """Add any filled orders not seen before, oldest first. Returns the book."""
    book = {**empty(), **(book or {})}
    seen = set(book["seen"])
    fresh = [o for o in orders if str(o.id) not in seen and crypto.text(o.status) == "filled"
             and float(o.filled_qty or 0) > 0 and str(o.filled_at)[:10] >= START_DAY]
    for order in sorted(fresh, key=lambda o: str(o.filled_at)):
        add_fill(book, order)
        book["seen"].append(str(order.id))
    book["seen"] = book["seen"][-MAX_SEEN:]
    book["trades"] = book["trades"][-MAX_TRADES:]
    return book


def recent_orders(trading):
    """Closed orders of the last 10 days. A run every few days would still catch everything."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500,
                               after=datetime.now(timezone.utc) - timedelta(days=10))
    return trading.get_orders(filter=request)


def scoreboard(trades, log_rows):
    """How the trades the AI would have vetoed did, against the ones it approved."""
    from ai_analyst import APPROVE, is_real_veto
    decided = {row.get("order_id"): row for row in log_rows
               if row.get("event") == "ORDER" and row.get("side") == "buy" and row.get("ai_decision")}
    groups = {"would_veto": [], "approved": []}
    for trade in trades:
        row = decided.get(trade["entry_order"])
        if row is None:
            continue
        if is_real_veto(row["ai_decision"], row.get("ai_reason", "")):
            groups["would_veto"].append(trade)
        elif row["ai_decision"] == APPROVE:
            groups["approved"].append(trade)

    def summary(rows):
        if not rows:
            return {"trades": 0, "win_rate": None, "expectancy_pct": None, "pnl": 0.0}
        return {"trades": len(rows), "win_rate": round(sum(r["pnl"] > 0 for r in rows) / len(rows), 4),
                "expectancy_pct": round(sum(r["ret"] for r in rows) / len(rows), 5),
                "pnl": round(sum(r["pnl"] for r in rows), 2)}

    errors = sum(1 for row in log_rows if row.get("event") == "AI_DECISION" and row.get("ai_decision") == "VETO"
                 and not is_real_veto("VETO", row.get("ai_reason", "")))
    vetoes = [row for row in log_rows if row.get("event") == "AI_DECISION" and is_real_veto(row.get("ai_decision"), row.get("ai_reason", ""))]
    from ai_analyst import mode
    return {"mode": mode(), "on": config.USE_AI_ANALYST, "would_veto": summary(groups["would_veto"]),
            "approved": summary(groups["approved"]), "ai_errors": errors,
            "latest": [{"time_utc": r.get("time_utc", ""), "symbol": r.get("symbol", ""), "reason": r.get("ai_reason", "")}
                       for r in vetoes[-10:]][::-1]}
