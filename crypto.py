"""
crypto.py  -  the crypto side of the bot. PAPER ONLY, on the same account as the stocks.

OFF until config.CRYPTO_ENABLED is True. It uses the same strategy, the same risk
rules and the same AI check as the stocks in bot.py. What is different:

  * Crypto trades 24/7, so there is no market-open check.
  * Orders use time_in_force GTC, never DAY.
  * Quantities can be fractional.
  * Alpaca writes pairs as BTC/USD in orders but may write BTCUSD in positions.
  * One trade per symbol per day, and the daily trade limit, count UTC days. Crypto
    has its own limit, so crypto trades never use up the stocks' trades.
  * Signals come from days that finished at midnight UTC.

The kill switch is shared with stocks: bot.py works it out and passes it in.
Stop-loss sells always go through, exactly as they do for stocks.
"""

import time
from datetime import datetime, timedelta, timezone

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import ai_analyst
import config
import risk
from data import get_crypto_daily_bars, get_crypto_latest_price
from strategy import BUY, SELL, latest_signal

NOT_A_TRADE = {"canceled", "rejected", "expired"}
QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BTC")
TEST_ORDER_DOLLARS = 10


def text(value):
    return str(getattr(value, "value", value) or "")


def money(x):
    return f"${x:,.2f}"


def amount(qty):
    """A coin amount without trailing zeros, e.g. 0.0641."""
    return f"{qty:.8f}".rstrip("0").rstrip(".")


def utc_midnight():
    """The start of today's crypto day."""
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def normalize(symbol):
    """Write any pair the way orders do: BTCUSD becomes BTC/USD."""
    symbol = str(symbol).upper()
    if "/" in symbol:
        return symbol
    for quote in QUOTE_CURRENCIES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


def is_crypto(item):
    """True for an Alpaca order or position that is crypto."""
    return text(getattr(item, "asset_class", "")) == "crypto" or "/" in str(item.symbol)


def orders_today(trading):
    """Crypto orders placed since midnight UTC."""
    request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=utc_midnight(), limit=500)
    return [o for o in trading.get_orders(filter=request)
            if is_crypto(o) and text(o.status) not in NOT_A_TRADE]


def place_order(trading, symbol, side, qty):
    """Submit a PAPER market order. Returns (order_id, status, fill_price or None)."""
    request = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC)
    order = trading.submit_order(order_data=request)
    status = text(order.status)
    for _ in range(5):  # give the paper exchange up to ~5 seconds to fill it
        if status == "filled":
            break
        time.sleep(1)
        try:
            order = trading.get_order_by_id(order.id)
            status = text(order.status)
        except Exception:
            break
    fill = float(order.filled_avg_price) if order.filled_avg_price else None
    return str(order.id), status, fill


def already_logged(todays_log, event, symbol, signal_date, decision=None):
    for row in todays_log:
        if row["event"] == event and row["symbol"] == symbol and row["signal_date"] == signal_date:
            if decision is None or row["ai_decision"] == decision:
                return True
    return False


def run(trading, equity, buying_power, killed, dry_run, todays_log, log_event):
    """One crypto run. todays_log holds the log rows written since midnight UTC."""
    print("\n=== Crypto · runs 24/7 · days count in UTC ===")
    todays_orders = orders_today(trading)
    trades_today = len(todays_orders)
    traded_today = {normalize(o.symbol) for o in todays_orders}
    positions = {normalize(p.symbol): p for p in trading.get_all_positions() if is_crypto(p)}
    print(f"Crypto trades today: {trades_today} of {config.MAX_TRADES_PER_DAY}"
          f" · Kill switch: {'ON, no new trades today' if killed else 'off'}")

    # --- Stop-loss on every crypto position ------------------------------------
    print("\nCrypto stop-loss check:")
    if not positions:
        print("  No open crypto positions.")
    sold_now = set()
    for symbol, position in positions.items():
        qty = float(position.qty)
        entry = float(position.avg_entry_price)
        price = float(position.current_price or 0)
        if qty <= 0:
            continue
        line = f"  {symbol}: {amount(qty)}, bought {money(entry)}, now {money(price)} ({price / entry - 1:+.1%})"
        if not risk.stop_loss_hit(entry, price):
            print(f"{line} · stop at {money(risk.stop_price(entry))} · OK")
            continue
        if dry_run:
            print(f"{line} · STOP-LOSS HIT, would sell {amount(qty)}")
            continue
        details = (f"stop-loss: {price / entry - 1:+.1%} from buy price {money(entry)} "
                   f"(limit -{config.STOP_LOSS_PCT:.0%})")
        order_id, status, fill = place_order(trading, symbol, OrderSide.SELL, qty)
        log_event(event="ORDER", symbol=symbol, side="sell", qty=qty, price=fill or price,
                  order_id=order_id, order_status=status, details=details, equity=equity)
        print(f"{line} · STOP-LOSS HIT → SOLD {amount(qty)} ({status})")
        trades_today += 1
        traded_today.add(symbol)
        sold_now.add(symbol)

    # --- Signals --------------------------------------------------------------
    history_start = datetime.now(timezone.utc) - timedelta(days=int(config.SLOW_SMA_DAYS * 1.6) + 60)
    bars = get_crypto_daily_bars(config.CRYPTO_SYMBOLS, history_start)
    candidates = []
    print("\nCrypto signals:")
    for symbol in config.CRYPTO_SYMBOLS:
        prices = bars.get(symbol)
        if prices is None or len(prices) < config.SLOW_SMA_DAYS + 1:
            print(f"  {symbol}: not enough price history, skipped")
            continue
        signal = latest_signal(prices)
        holding = symbol in positions and symbol not in sold_now
        action, note = None, ""
        if signal["signal"] == BUY:
            action, note = (None, "already holding") if holding else (BUY, "")
        elif signal["signal"] == SELL:
            action, note = (SELL, "") if holding else (None, "no position, nothing to sell")
        elif holding and signal["trend"] == "DOWN":
            action, note = SELL, "holding while the trend is down (sell signal was missed)"

        print(f"  {symbol:8} {signal['signal']:4} {signal['reason']}"
              f" [close of {signal['date']}]" + (f" · {note}" if note else ""))

        logged_signal = signal["signal"] if signal["signal"] != "HOLD" else (SELL if action else None)
        if logged_signal and not dry_run and not already_logged(todays_log, "SIGNAL", symbol, signal["date"]):
            log_event(event="SIGNAL", symbol=symbol, signal=logged_signal, signal_date=signal["date"],
                      details=f"{signal['reason']}" + (f"; {note}" if note else ""), equity=equity)
        if action:
            candidates.append((symbol, action, signal, prices))

    # Sells first (they free up cash and reduce risk), then buys.
    candidates.sort(key=lambda c: 0 if c[1] == SELL else 1)
    if candidates:
        print("\nCrypto orders:")
    for symbol, action, signal, prices in candidates:
        placed = handle_candidate(trading, symbol, action, signal, prices, positions, equity,
                                  buying_power, todays_log, dry_run, killed, trades_today,
                                  traded_today, log_event)
        if placed:
            trades_today += 1


def handle_candidate(trading, symbol, action, signal, prices, positions, equity, buying_power,
                     todays_log, dry_run, killed, trades_today, traded_today, log_event):
    """Run one BUY/SELL through the risk rules and the AI. Returns True if an order was placed."""
    date = signal["date"]
    side_word = "buy" if action == BUY else "sell"

    def block(reason):
        print(f"  {symbol}: {side_word} BLOCKED · {reason}")
        if not dry_run and not already_logged(todays_log, "BLOCKED", symbol, date):
            log_event(event="BLOCKED", symbol=symbol, signal=action, signal_date=date,
                      side=side_word, details=reason, equity=equity)

    if symbol in traded_today:
        print(f"  {symbol}: already traded today (UTC), skipping")
        return

    # --- Rule 1: size (and what we would sell) ---------------------------------
    if action == BUY:
        price = get_crypto_latest_price(symbol, fallback=signal["close"])
        qty = risk.max_crypto_qty(equity, price)
        if qty <= 0:
            return block("could not work out a position size")
        size_note = f"{amount(qty)} ≈ {money(qty * price)} ({qty * price / equity:.1%} of equity)"
    else:
        position = positions[symbol]
        qty = float(position.qty)
        price = float(position.current_price or signal["close"])
        size_note = f"all {amount(qty)} ≈ {money(qty * price)}"

    if dry_run:
        print(f"  {symbol}: would {side_word} {size_note}" +
              (" (but the kill switch is ON)" if killed else ""))
        return
    # --- Rule 4 and Rule 3 -------------------------------------------------------
    if killed:
        return block(f"kill switch is on (equity fell {config.KILL_SWITCH_PCT:.0%}+ today)")
    if risk.trades_left(trades_today) == 0:
        return block(f"daily crypto trade limit reached ({config.MAX_TRADES_PER_DAY})")
    if action == BUY and qty * price > buying_power:
        return block(f"not enough cash: crypto can't use margin ({money(buying_power)} available)")

    # --- Optional AI analyst: approve or veto only -----------------------------
    if already_logged(todays_log, "AI_DECISION", symbol, date, decision=ai_analyst.VETO):
        print(f"  {symbol}: AI already vetoed this signal today, skipping")
        return
    decision, ai_reason = ai_analyst.review(symbol, action, signal["reason"], prices)
    if decision != ai_analyst.OFF:
        log_event(event="AI_DECISION", symbol=symbol, signal=action, signal_date=date,
                  ai_decision=decision, ai_reason=ai_reason, equity=equity)
        print(f"  {symbol}: AI analyst says {decision} · {ai_reason}")
    if decision == ai_analyst.VETO:
        return

    # --- Place the PAPER order ------------------------------------------------
    side = OrderSide.BUY if action == BUY else OrderSide.SELL
    order_id, status, fill = place_order(trading, symbol, side, qty)
    log_event(event="ORDER", symbol=symbol, signal=action, signal_date=date, side=side_word,
              qty=float(qty), price=fill or price, order_id=order_id, order_status=status,
              ai_decision=decision, ai_reason=ai_reason,
              details=f"strategy {action}: {size_note} · {signal['reason']}", equity=equity)
    fill_text = f" at {money(fill)}" if fill else ""
    print(f"  {symbol}: {side_word.upper()} {size_note} → order {status}{fill_text}")
    traded_today.add(symbol)
    return True


def run_test_order(trading, symbol, equity, killed, log_event):
    """A tiny manual paper buy, to check that crypto orders reach your Alpaca dashboard."""
    symbol = normalize(symbol)
    print(f"\nTest order: buy about {money(TEST_ORDER_DOLLARS)} of {symbol} (not a strategy signal; the AI is not asked).")
    if killed:
        print("❌ Not placed: the kill switch is on today.")
        return 1
    if risk.trades_left(len(orders_today(trading))) == 0:
        print(f"❌ Not placed: today's crypto trade limit ({config.MAX_TRADES_PER_DAY}) is already used.")
        return 1
    price = get_crypto_latest_price(symbol, fallback=0.0)
    if price <= 0:
        print(f"❌ Not placed: could not get a price for {symbol}. Check the pair.")
        return 1

    qty = round(TEST_ORDER_DOLLARS / price, 8)
    order_id, status, fill = place_order(trading, symbol, OrderSide.BUY, qty)
    log_event(event="ORDER", symbol=symbol, side="buy", qty=qty, price=fill or price,
              order_id=order_id, order_status=status, equity=equity,
              details="manual test order (not a strategy signal)")
    print(f"✅ Paper order sent: buy {amount(qty)} {symbol} · status: {status}" + (f" at {money(fill)}" if fill else ""))
    print(f"   Order id: {order_id}")
    print("   Now look for it in your Alpaca dashboard (Paper account) under Orders.")
    return 0
