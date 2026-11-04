"""
stops.py  -  keeps exactly one stop order waiting at Alpaca for every stock position.

A stop order sits at the broker and sells the moment the price trades through it, so it
doesn't have to wait for the next 15-minute run. Used when config.STOP_EXECUTION is "alpaca".

  * Whole shares use a good-till-cancelled stop. Parts of a share can only use a day stop,
    so those expire each evening and the day's first run places them again.
  * Every run checks each position has exactly one working stop, and fixes it if not.
  * A position that needs selling for another reason has its stop cancelled first.
  * The 15-minute check stays as a backup, for a position with no working stop.

Alpaca refuses a sell stop above the market price. A position already under its stop gets
no stop order: the backup check sells it instead.
"""

import uuid

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import StopOrderRequest

import config
import crypto
import risk


def is_stop(order):
    kind = getattr(order, "order_type", None) or getattr(order, "type", None)
    return crypto.text(kind) in ("stop", "stop_limit")


def by_symbol(orders):
    """The open stop orders, grouped by symbol."""
    found = {}
    for order in orders:
        if is_stop(order):
            found.setdefault(order.symbol, []).append(order)
    return found


def level_for(record, entry):
    """Where the stop sits. Approved ATR stops use their saved level, otherwise 7% below the buy price.
    A stop fix (tighter or trailing, see fixes.py) can only move it closer, never further away."""
    if config.USE_ATR_STOPS and record and record.get("stop"):
        return float(record["stop"])
    level = risk.stop_price(entry)
    if record and record.get("fix_stop"):
        level = max(level, float(record["fix_stop"]))
    return level


def order_id_for(sleeve):
    return f"tach-{sleeve or 'legacy'}-stop-{uuid.uuid4().hex[:12]}"


def is_whole(qty):
    return float(qty).is_integer()


def request_for(symbol, qty, level, sleeve):
    """Alpaca only takes day orders for parts of a share."""
    return StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, stop_price=round(level, 2),
                            time_in_force=TimeInForce.GTC if is_whole(qty) else TimeInForce.DAY,
                            client_order_id=order_id_for(sleeve))


def matches(order, qty, level):
    """A working stop is right if it covers the whole position at the right price."""
    try:
        return abs(float(order.qty) - qty) < 1e-9 and abs(float(order.stop_price) - round(level, 2)) < 0.005
    except (TypeError, ValueError):
        return False


def expected_gap(record, qty, today, first_run):
    """No stop is normal for a brand new position, or a part-share one at the start of a day."""
    return bool(record and record.get("entry_date") == today) or (first_run and not is_whole(qty))


def sync(trading, positions, working, skip, records, today, first_run, dry_run):
    """Make every position have exactly one right stop. Returns (what was fixed, placed count)."""
    fixes, placed = [], 0
    for symbol, position in positions.items():
        qty = float(position.qty)
        if qty <= 0 or symbol in skip:
            continue
        record = records.get(symbol)
        entry, price = float(position.avg_entry_price), float(position.current_price or 0)
        level = level_for(record, entry)
        current = working.get(symbol, [])
        keeper = next((order for order in current if matches(order, qty, level)), None)
        extras = [order for order in current if order is not keeper]
        if extras:
            fixes.append(f"{symbol}: cancelled {len(extras)} extra stop{'s' if len(extras) > 1 else ''}")
            if not dry_run:
                for order in extras:
                    trading.cancel_order_by_id(order.id)
        if keeper is not None or (price and price <= level):
            continue
        request = request_for(symbol, qty, level, (record or {}).get("sleeve", ""))
        if dry_run:
            print(f"  {symbol}: would place a stop for {qty:g} at {round(level, 2):,.2f}")
            continue
        try:
            trading.submit_order(order_data=request)
        except Exception as error:
            fixes.append(f"{symbol}: stop refused, the backup check stays on it ({str(error)[:80]})")
            continue
        placed += 1
        if not expected_gap(record, qty, today, first_run) and not extras:
            fixes.append(f"{symbol}: placed a missing stop at {round(level, 2):,.2f}")
    return fixes, placed
