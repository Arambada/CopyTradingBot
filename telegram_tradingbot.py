"""
Telegram → MT5 Trade Copier Bot
Monitors GTMO VIP Telegram channel and auto-executes trades on MT5 (Vantage)

Requirements:
    pip install telethon MetaTrader5 groq

Setup:
    Fill in your credentials in the CONFIG section below.
"""

import asyncio
import re
import os
import sys
import json
import time
import logging
from dotenv import load_dotenv
from telethon import TelegramClient, events
import MetaTrader5 as mt5
from groq import Groq

load_dotenv()

TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])      # From my.telegram.org
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]       # From my.telegram.org
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]         # Channel name or @username

MT5_LOGIN = int(os.environ["MT5_LOGIN"])                  # Your MT5 account number
MT5_PASSWORD = os.environ["MT5_PASSWORD"]                 # Your MT5 password
# Use "Vantage-Demo" for demo, "Vantage-Live" for live
MT5_SERVER = os.environ["MT5_SERVER"]
# Full path to terminal64.exe (helps mt5.initialize attach to the right terminal)
MT5_PATH = os.getenv("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

# From console.groq.com/keys
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
# Groq model used for parsing (override in .env if desired)
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

LOT_SIZE = float(os.getenv("LOT_SIZE", "0.01"))           # Trade size per signal
USE_TP1_ONLY = os.getenv("USE_TP1_ONLY", "True") == "True"  # True: one order at TP1; False: split TP1 & TP2 into two orders
SYMBOL = os.getenv("SYMBOL", "XAUUSD")                    # Gold symbol on Vantage
# Hours before an unfilled pending order auto-cancels (0 = never, keep GTC)
PENDING_EXPIRY_HOURS = float(os.getenv("PENDING_EXPIRY_HOURS", "12"))

# SYMBOL_EXPIRATION_* bit flags (not exposed by the MetaTrader5 module)
_EXPIRATION_SPECIFIED = 4

# Ensure emoji/unicode in log messages don't crash on Windows' default cp1252 console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("trade_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Groq client for AI parsing
ai_client = Groq(api_key=GROQ_API_KEY)

SIGNAL_SYSTEM_PROMPT = """You read messages from a forex/gold trading channel and return ONLY a JSON object describing what to do. There are three cases.

CASE 1 — a NEW trade signal.
The headline looks like "buy re-entry 4260 - 4245" or "sell 3950 - 3970".
The two headline numbers are an ENTRY PRICE ZONE (a range to enter in), NOT an
entry plus a stop loss:
  * one number is the lower bound of the entry zone -> "entry_low"
  * the other number is the upper bound of the entry zone -> "entry_high"
  * sort them so entry_low <= entry_high regardless of the order written
If only a single entry price is given, put it in BOTH entry_low and entry_high.
The STOP LOSS comes ONLY from the "SL:" line — never from a headline number.
The take-profit targets come from the "TP:" lines.
Return:
{
  "action": "open",
  "instrument": "XAUUSD",
  "direction": "BUY" or "SELL",
  "entry_low": float or null,
  "entry_high": float or null,
  "sl": float or null,
  "tp1": float or null,
  "tp2": float or null,
  "tp3": float or null
}

CASE 2 — an ADJUSTMENT to a trade that is already running (the trader moves the stop
loss and/or take profit). Examples: "SL to 4250", "move stop loss 3960", "change SL 1234",
"TP to 4300", "take profit 4310", "SL 4250 TP 4320".
Return:
{
  "action": "modify",
  "new_sl": float or null,
  "new_tp": float or null
}
Only fill a field when an explicit NUMERIC value is given. Do NOT infer breakeven,
"entry", or any non-numeric stop — use null when no number is present.

CASE 3 — anything else (chat, commentary, results, questions).
Return: {"action": "none"}

Return only the JSON object, no other text."""


def parse_signal_with_ai(message: str) -> dict | None:
    """Use Groq (Llama) to extract trade details from a signal message."""
    try:
        response = ai_client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SIGNAL_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        )
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"AI parsing error: {e}")
        return None


# ── Signal-message → MT5 ticket map ──────────────────────────────────────────
# Lets a later reply that adjusts SL/TP be routed to the exact trade it opened.
# Persisted to disk so the mapping survives a bot restart.
TRADE_MAP_FILE = "trade_map.json"
# One signal can open several tickets (e.g. one order per edge of an entry zone),
# so each message id maps to a LIST of MT5 tickets.
_trade_map: dict[str, list[int]] = {}


def _load_trade_map() -> None:
    global _trade_map
    try:
        with open(TRADE_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    # Normalise: tolerate an older format that stored a single int per message.
    _trade_map = {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}


def _record_trades(msg_id: int, tickets: list[int]) -> None:
    """Remember that Telegram message ``msg_id`` opened the given MT5 tickets."""
    _trade_map[str(msg_id)] = tickets
    try:
        with open(TRADE_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(_trade_map, f)
    except OSError as e:
        log.warning(f"Could not persist trade map: {e}")


def _ensure_mt5() -> bool:
    """Reconnect MT5 if the terminal link dropped during a long session."""
    if not mt5.terminal_info():
        log.warning("MT5 disconnected, reconnecting...")
        if not connect_mt5():
            log.error("Could not reconnect to MT5!")
            return False
    return True


def connect_mt5() -> bool:
    """Initialize and log in to MT5.

    Tries two strategies in order so the bot survives an already-running
    terminal as well as a cold start:
      A) Attach to a terminal that's already open and logged in (no path/creds).
      B) Launch the terminal fresh from MT5_PATH with credentials.
    timeout is in milliseconds.
    """
    # Attempt A — attach to an already-running terminal.
    if mt5.initialize(timeout=60_000):
        log.info("MT5 initialize: attached to running terminal.")
    else:
        attach_err = mt5.last_error()
        log.warning(f"MT5 attach failed ({attach_err}); trying to launch terminal...")

        # Attempt B — launch the terminal fresh from MT5_PATH with credentials.
        init_kwargs = {
            "login": MT5_LOGIN,
            "password": MT5_PASSWORD,
            "server": MT5_SERVER,
            "timeout": 60_000,
        }
        if MT5_PATH and os.path.exists(MT5_PATH):
            init_kwargs["path"] = MT5_PATH
        else:
            log.warning(
                f"MT5_PATH not found ({MT5_PATH}); letting MetaTrader5 auto-detect the terminal.")

        if not mt5.initialize(**init_kwargs):
            err = mt5.last_error()
            log.error(f"MT5 initialize failed: {err}")
            if err and err[0] == -10005:
                log.error(
                    "IPC timeout: Python could not talk to the MT5 terminal.")
                log.error(
                    "If you can log into MT5 manually but the bot still times out, MT5 and "
                    "Python are likely running at DIFFERENT privilege levels. Either close "
                    "MT5 completely and let the bot launch it, or run both as the same user "
                    "(both normal, or both 'Run as Administrator').")
                log.error(
                    "Also confirm Tools → Options → Expert Advisors → 'Allow algorithmic "
                    "trading' is enabled. Then re-run.")
            return False
        log.info("MT5 initialize: launched terminal from path.")

    # If the attached terminal is already logged into the target account, skip the
    # explicit login(). Re-logging in forces a server reconnect that can IPC-timeout
    # on live accounts (the terminal is already connected — no need to redo it).
    account = mt5.account_info()
    if account is not None and account.login == MT5_LOGIN:
        log.info(f"Already logged into account {MT5_LOGIN}; skipping re-login.")
    elif not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        err = mt5.last_error()
        log.error(f"MT5 login failed: {err}")
        if err and err[0] == -10005:
            log.error(
                f"Login IPC timeout for account {MT5_LOGIN} on '{MT5_SERVER}'. "
                "Log into this exact account in the MT5 terminal manually first, then "
                "re-run — the bot will attach to it without re-logging in. Also double-check "
                "the server name matches the terminal exactly (including spaces, e.g. "
                "'VantageInternational-Live 10').")
        mt5.shutdown()
        return False
    account = mt5.account_info()
    log.info(
        f"✅ MT5 connected — {account.name} | Balance: {account.balance} {account.currency}")
    return True


def _normalize_stops(direction, price, sl, tp, min_dist, digits):
    """Validate/round a signal's SL & TP against the live price.

    For a correct-side stop that sits too close to price (inside the broker's
    minimum stop distance), the stop is pushed out to the minimum distance so the
    order is accepted. For a stop on the WRONG side of the market (e.g. a BUY whose
    TP is already below price), returns ``False`` for that value to signal the
    caller to skip the trade. Returns ``(sl, tp)`` where each is a rounded float,
    ``None`` (not provided), or ``False`` (invalid → skip).
    """
    def fix(level, is_sl):
        if not level:
            return None
        level = float(level)
        if direction == "BUY":
            # BUY: SL below price, TP above price.
            want_below = is_sl
        else:
            # SELL: SL above price, TP below price.
            want_below = not is_sl

        if want_below:
            if level >= price:
                log.warning(
                    f"{'SL' if is_sl else 'TP'} {level} is not below price {price} "
                    f"for a {direction} — skipping trade.")
                return False
            level = min(level, price - min_dist)   # push out if too close
        else:
            if level <= price:
                log.warning(
                    f"{'SL' if is_sl else 'TP'} {level} is not above price {price} "
                    f"for a {direction} — skipping trade.")
                return False
            level = max(level, price + min_dist)   # push out if too close
        return round(level, digits)

    return fix(sl, is_sl=True), fix(tp, is_sl=False)


# Human-readable labels for logging
_ORDER_LABELS = {
    mt5.ORDER_TYPE_BUY: "BUY (market)",
    mt5.ORDER_TYPE_SELL: "SELL (market)",
    mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT",
    mt5.ORDER_TYPE_BUY_STOP: "BUY STOP",
    mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT",
    mt5.ORDER_TYPE_SELL_STOP: "SELL STOP",
}


def _choose_order(direction, market, entry, min_dist):
    """Decide the MT5 order type and the price to validate stops against.

    The signal gives a single entry price. The order type is picked so the order
    waits at that entry price and fills when the market reaches it.
    Returns ``(order_type, exec_price, is_pending)``.

    - No entry price  → market order at ``market``.
    - BUY:  entry below market → BUY LIMIT;  entry above market → BUY STOP.
    - SELL: entry above market → SELL LIMIT;  entry below market → SELL STOP.
    - If the entry is within ``min_dist`` of the market (price is basically already
      there), fall back to a market order so the broker doesn't reject the pending
      price for being too close.
    """
    market_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

    if entry is None:
        return market_type, market, False

    # Price essentially at the entry already → take it at market.
    if abs(market - entry) < min_dist:
        return market_type, market, False

    if direction == "BUY":
        otype = mt5.ORDER_TYPE_BUY_LIMIT if market > entry else mt5.ORDER_TYPE_BUY_STOP
    else:  # SELL
        otype = mt5.ORDER_TYPE_SELL_LIMIT if market < entry else mt5.ORDER_TYPE_SELL_STOP

    return otype, entry, True


def _entry_prices(signal: dict, direction: str) -> list:
    """Entry price(s) to place orders at, derived from the signal's entry zone.

    Uses the BEST edge of the zone for the trade's side — the lower bound for a
    BUY, the higher bound for a SELL — and ignores the other bound. A single
    price (or both bounds equal) -> that price. No entry given -> one market
    order (represented as ``[None]``).
    """
    lo = signal.get("entry_low")
    hi = signal.get("entry_high")
    if lo is None and hi is None:
        e = signal.get("entry")  # back-compat with the old single-entry field
        return [float(e)] if e else [None]
    prices = [float(v) for v in (lo, hi) if v is not None]
    if not prices:
        return [None]
    return [min(prices)] if direction == "BUY" else [max(prices)]


def _tp_targets(signal: dict) -> list:
    """Take-profit target(s); one order is placed per target (same entry & SL).

    With ``USE_TP1_ONLY`` only TP1 is used (a single order). Otherwise TP1 and
    TP2 each get their own order. Missing targets are skipped; if no TP is given
    at all, a single order with no TP is placed (``[None]``).
    """
    tps = [float(signal[k]) for k in ("tp1", "tp2") if signal.get(k) is not None]
    if USE_TP1_ONLY:
        tps = tps[:1]
    return tps or [None]


def place_trade(signal: dict) -> list[int]:
    """Place order(s) for a parsed signal and return the MT5 ticket(s) opened.

    The entry is the best edge of the zone for the trade's side (lower bound for
    BUY, higher bound for SELL; the other bound is ignored). When TP2 is present
    and ``USE_TP1_ONLY`` is off, TWO orders are placed at that same entry and SL
    — one targeting TP1, the other TP2. Returns the list of accepted tickets.
    """
    direction = signal.get("direction", "").upper()
    if direction not in ("BUY", "SELL"):
        log.warning("Invalid direction, skipping trade.")
        return []

    sl = signal.get("sl")
    entry = _entry_prices(signal, direction)[0]

    tickets = []
    for tp in _tp_targets(signal):
        ticket = _send_order(direction, entry, sl, tp)
        if ticket:
            tickets.append(ticket)
    return tickets


def _send_order(direction: str, entry, sl, tp) -> int | bool:
    """Place one market/pending order. Returns the MT5 ticket, or ``False``."""
    entry = float(entry) if entry else None

    # Get symbol metadata and current market price
    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    if info is None or tick is None:
        log.error(f"Cannot get symbol info/tick for {SYMBOL}")
        return False

    market = tick.ask if direction == "BUY" else tick.bid

    # Broker's minimum allowed distance between price and SL/TP (in price units).
    # A small buffer is added so we never sit exactly on the boundary.
    min_dist = info.trade_stops_level * info.point
    min_dist += info.point  # one extra point of safety

    # Choose order type (market vs pending limit/stop) and the price to use.
    order_type, exec_price, is_pending = _choose_order(
        direction, market, entry, min_dist)
    exec_price = round(exec_price, info.digits)

    # SL/TP are validated against the order's entry price (the pending price for
    # pending orders, or the market price for market orders).
    sl, tp = _normalize_stops(direction, exec_price, sl, tp, min_dist, info.digits)
    if sl is False or tp is False:
        # A stop was on the wrong side — skip rather than send an order that would
        # be rejected (retcode 10016) or fill with broken stops.
        return False

    request = {
        "action":       mt5.TRADE_ACTION_PENDING if is_pending else mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       LOT_SIZE,
        "type":         order_type,
        "price":        exec_price,
        "magic":        234000,      # Bot identifier
        "comment":      "GTMO_BOT",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN if is_pending else mt5.ORDER_FILLING_IOC,
    }
    if not is_pending:
        request["deviation"] = 20    # Max slippage in points (market orders only)
    elif PENDING_EXPIRY_HOURS > 0:
        # Auto-cancel the pending order if it hasn't filled within N hours.
        if info.expiration_mode & _EXPIRATION_SPECIFIED:
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = int(
                time.time() + PENDING_EXPIRY_HOURS * 3600)
        else:
            log.warning(
                f"{SYMBOL} broker does not support timed expiration "
                f"(mode {info.expiration_mode}); leaving order as GTC.")

    if sl:
        request["sl"] = sl
    if tp:
        request["tp"] = tp

    label = _ORDER_LABELS.get(order_type, str(order_type))
    result = mt5.order_send(request)

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        kind = "pending order placed" if is_pending else "trade placed"
        expiry = ""
        if is_pending and request.get("type_time") == mt5.ORDER_TIME_SPECIFIED:
            expiry = f" | expires in {PENDING_EXPIRY_HOURS}h"
        log.info(
            f"✅ {kind}! Ticket: {result.order} | {label} {SYMBOL} @ {exec_price} "
            f"| SL: {sl} | TP: {tp} | market: {market}{expiry}")
        return result.order
    else:
        log.error(
            f"❌ Order failed — retcode: {result.retcode} | {result.comment} "
            f"| tried {label} @ {exec_price} (market {market}) SL {sl} TP {tp}")
        return False


def modify_trade(ticket: int, new_sl, new_tp) -> bool:
    """Adjust SL and/or TP on an existing bot trade.

    Works for both a filled position (``TRADE_ACTION_SLTP``) and an unfilled
    pending order (``TRADE_ACTION_MODIFY``). Whichever of SL/TP is not supplied
    is left at its current value. Returns True on a successful modification.
    """
    if new_sl is None and new_tp is None:
        log.warning("Modify request had no numeric SL or TP; nothing to do.")
        return False

    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    if info is None or tick is None:
        log.error(f"Cannot get symbol info/tick for {SYMBOL}")
        return False
    min_dist = info.trade_stops_level * info.point + info.point

    positions = mt5.positions_get(ticket=ticket)
    orders = mt5.orders_get(ticket=ticket)

    if positions:
        pos = positions[0]
        direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        ref_price = tick.ask if direction == "BUY" else tick.bid
        cur_sl, cur_tp = pos.sl, pos.tp
        request = {"action": mt5.TRADE_ACTION_SLTP, "symbol": SYMBOL, "position": ticket}
    elif orders:
        od = orders[0]
        buy_types = (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)
        direction = "BUY" if od.type in buy_types else "SELL"
        ref_price = od.price_open
        cur_sl, cur_tp = od.sl, od.tp
        request = {
            "action": mt5.TRADE_ACTION_MODIFY, "symbol": SYMBOL, "order": ticket,
            "price": od.price_open, "type_time": mt5.ORDER_TIME_GTC,
        }
    else:
        log.warning(
            f"Trade {ticket} is no longer open (closed or SL/TP already hit); cannot modify.")
        return False

    # Keep the value the trader didn't mention; validate sides + min distance.
    target_sl = float(new_sl) if new_sl is not None else (cur_sl or None)
    target_tp = float(new_tp) if new_tp is not None else (cur_tp or None)
    valid_sl, valid_tp = _normalize_stops(
        direction, ref_price, target_sl, target_tp, min_dist, info.digits)
    if valid_sl is False or valid_tp is False:
        log.error(
            f"New SL/TP for ticket {ticket} is on the wrong side of price; modify skipped.")
        return False

    if valid_sl:
        request["sl"] = valid_sl
    if valid_tp:
        request["tp"] = valid_tp

    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(
            f"✏️ Trade {ticket} updated — SL: {request.get('sl')} | TP: {request.get('tp')}")
        return True
    log.error(
        f"❌ Modify failed for {ticket} — retcode: {result.retcode} | {result.comment}")
    return False


async def main():
    log.info("🤖 Starting Telegram → MT5 Trade Copier Bot...")

    # Connect to MT5
    if not connect_mt5():
        log.error("Failed to connect to MT5. Check credentials and server name.")
        return

    # Restore the signal-message → ticket map from the previous run.
    _load_trade_map()

    # Connect to Telegram
    client = TelegramClient(
        "trade_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    log.info("✅ Telegram connected")

    # Resolve channel
    channel = await client.get_entity(TELEGRAM_CHANNEL)
    log.info(f"✅ Listening to channel: {channel.title}")

    @client.on(events.NewMessage(chats=channel))
    async def handle_message(event):
        message = event.message.message
        if not message:
            return

        log.info(f"📨 New message: {message[:80]}...")

        # Parse with AI
        parsed = parse_signal_with_ai(message)
        if not parsed:
            return

        # Back-compat: older prompt returned {"is_signal": true} for opens.
        action = parsed.get("action") or ("open" if parsed.get("is_signal") else "none")

        if action == "open":
            log.info(f"📊 Signal detected: {parsed}")
            if not _ensure_mt5():
                return
            tickets = place_trade(parsed)
            if tickets:
                # Remember which message opened these trades so a reply can adjust them.
                _record_trades(event.message.id, tickets)

        elif action == "modify":
            # Only act when this is a reply to a signal we actually opened trades for.
            orig_id = event.message.reply_to_msg_id
            if not orig_id:
                log.info(
                    "SL/TP update, but it's not a reply to a signal — can't tell which "
                    "trade to modify; ignoring.")
                return
            tickets = _trade_map.get(str(orig_id))
            if not tickets:
                log.info(
                    f"SL/TP update replying to message {orig_id}, but no tracked trade "
                    "for it; ignoring.")
                return
            new_sl, new_tp = parsed.get("new_sl"), parsed.get("new_tp")
            log.info(
                f"✏️ SL/TP update for tickets {tickets}: new_sl={new_sl} new_tp={new_tp}")
            if not _ensure_mt5():
                return
            for ticket in tickets:
                modify_trade(ticket, new_sl, new_tp)

        else:
            log.info("Not a trade signal, ignoring.")

    log.info("👂 Listening for signals... (Press Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
        mt5.shutdown()
