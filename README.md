# Telegram -> MT5 Trade Copier Bot

Monitors a Telegram trading channel, parses each message with an AI model, and
automatically places / manages the corresponding trades on a MetaTrader 5
(Vantage) account.

```
Telegram channel -> Telethon -> Groq (Llama) parser -> MetaTrader5 terminal
   (signals)          (listener)     (extract intent)        (orders / SL-TP)
```

---

## How it works

1. **Listen** – `telethon` subscribes to the configured channel and fires on every new message.
2. **Parse** – each message is sent to Groq (Llama) which returns a JSON intent:
   - `open`   – a new trade signal
   - `modify` – an SL/TP adjustment to a trade already running
   - `none`   – chat / commentary (ignored)
3. **Execute** – the bot places or modifies orders on MT5 via the `MetaTrader5` package.

### Signal format to match the Telegram Channel

A signal headline such as `buy 4230 - 4235` is an **entry price zone**, not an
entry-plus-stop. The two numbers are the low/high bounds of where to enter; the
**stop loss is taken only from the `SL:` line**, and take-profits from the `TP:`
lines.

```
buy 4230 - 4235        entry zone (low–high)
SL: 4215               stop loss
TP: 4250 4270          take-profit targets
```

**Entry selection** – the bot enters at the *best edge* of the zone for the
trade's side and ignores the other bound:
- **BUY**  -> the **lower** bound (e.g. `4230`)
- **SELL** -> the **higher** bound (e.g. `3970`)

**Take-profit split** – controlled by `USE_TP1_ONLY`:
- `False` -> **two orders** at the same entry & SL, one targeting **TP1** and one **TP2**
- `True`  -> a single order at **TP1**

| Example signal | Orders placed (`USE_TP1_ONLY=False`) |
| --- | --- |
| `buy 4230 - 4235` / `SL: 4215` / `TP: 4250 4270` | BUY LIMIT @ 4230, SL 4215, TP 4250 **and** BUY LIMIT @ 4230, SL 4215, TP 4270 |

Each order uses the full `LOT_SIZE`, so a fully-filled two-TP signal opens
`2 × LOT_SIZE` of total exposure.

### Adjusting a live trade (SL / TP)

When the trader **replies** to their original signal with an adjustment
(e.g. `SL to 4250`, `TP 4310`), the bot routes the change to the exact trade(s)
that signal opened:

- The mapping of signal message → MT5 ticket(s) is stored in `trade_map.json`
  (persisted, so it survives restarts).
- Filled positions are modified with `TRADE_ACTION_SLTP`; unfilled pending
  orders with `TRADE_ACTION_MODIFY`.
- **Only numeric** SL/TP values are honoured (no "breakeven", no close/partial).
- The message **must be a reply** to the signal — standalone adjustments are
  ignored because there's no unambiguous way to know which trade they target.

### Other behaviour

- **Pending-order expiry** – unfilled pending orders auto-cancel after
  `PENDING_EXPIRY_HOURS` (if the broker supports timed expiration).
- **Auto-reconnect** – the MT5 link is re-established if it drops during a long
  session.
- **Stop validation** – SL/TP are checked for the correct side of price and
  pushed out to the broker's minimum stop distance if too close.

---

## Requirements

- **Windows** (the `MetaTrader5` Python package is Windows-only)
- **64-bit Python** — the package cannot talk to `terminal64.exe` from 32-bit
  Python. Python **3.11 / 3.12** are the safest; very new versions can lag the
  package's support window.
- A running, logged-in **MetaTrader 5** terminal (Vantage)
- A **Telegram API** id/hash from <https://my.telegram.org>
- A **Groq API key** from <https://console.groq.com/keys>

### Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install telethon MetaTrader5 groq python-dotenv
```

---

## Configuration

Settings are read from a `.env` file in the project root:

```ini
# Telegram (from my.telegram.org)
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHANNEL=https://t.me/your_channel

# MetaTrader 5 (Vantage)
MT5_LOGIN=12345678
MT5_PASSWORD='your-password'
MT5_SERVER=Metdatrader-Demo
# Optional: full path to terminal64.exe (auto-detected if omitted)
# MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

# Groq (from console.groq.com/keys)
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
# Optional model override
GROQ_MODEL=llama-3.3-70b-versatile

# Trade settings
LOT_SIZE=0.02
# True = one order at TP1; False = split TP1 & TP2 into two orders
USE_TP1_ONLY=False
SYMBOL=XAUUSD
# Hours before an unfilled pending order auto-cancels (0 = never / keep GTC)
PENDING_EXPIRY_HOURS=12
```

| Variable | Description |
| --- | --- |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Telegram app credentials |
| `TELEGRAM_CHANNEL` | Channel URL or `@username` to copy |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | MT5 account login (server name must match the terminal **exactly**, spaces included) |
| `MT5_PATH` | Optional path to `terminal64.exe` |
| `GROQ_API_KEY` / `GROQ_MODEL` | AI parser credentials / model |
| `LOT_SIZE` | Volume per order |
| `USE_TP1_ONLY` | `True` = single TP1 order; `False` = split TP1 & TP2 |
| `SYMBOL` | Instrument to trade (e.g. `XAUUSD`) |
| `PENDING_EXPIRY_HOURS` | Auto-cancel window for unfilled pendings |

> `.env`, `*.session`, `*.log`, and the virtualenv are git-ignored — never
> commit credentials.

---

## Running

1. Open MetaTrader 5 and log in to the account in `.env`.
2. Enable **Tools → Options → Expert Advisors → Allow Algo Trading** (the
   toolbar **Algo Trading** button should be green).
3. Start the bot:

```bash
python telegram_tradingbot.py
```

Expected startup:

```
🤖 Starting Telegram → MT5 Trade Copier Bot...
MT5 initialize: attached to running terminal.
✅ MT5 connected — <name> | Balance: …
✅ Telegram connected
✅ Listening to channel: <title>
👂 Listening for signals... (Press Ctrl+C to stop)
```

The bot runs indefinitely; press **Ctrl+C** to stop. Keep the PC awake (disable
sleep) for unattended 24/7 operation.

---

## Troubleshooting

### `(-10005, 'IPC timeout')` on connect

Python couldn't talk to the MT5 terminal. In order of likelihood:

1. **Admin / privilege mismatch** — MT5 was started "As Administrator" while
   Python runs as a normal user (or vice-versa). Windows blocks the IPC pipe
   across privilege levels. Fix: run **both** at the same level, or close MT5 and
   let the bot launch it.
2. **Algo trading disabled** — enable it (see *Running* above).
3. **Terminal not logged in / not open** — open MT5 and log in manually first.
4. **32-bit Python** — reinstall on 64-bit Python.

### `MT5 login failed: (-10005 …)` (connect succeeds, login times out)

The bot attaches to the running terminal but the explicit re-login to the live
account times out. Make sure MT5 is **already logged into the exact account** in
`.env`; the bot then skips the redundant re-login automatically. Confirm
`MT5_SERVER` matches the terminal's server name exactly (including spaces, e.g.
`VantageInternational-Live 10`).

### An SL/TP update was ignored

It must be sent as a **reply** to the original signal message, and contain a
**numeric** value. Standalone or non-numeric ("breakeven") adjustments are not
acted on.

---

## Project files

| File | Purpose |
| --- | --- |
| `telegram_tradingbot.py` | The bot (single module) |
| `.env` | Credentials & trade settings (git-ignored) |
| `trade_map.json` | Persisted signal-message → MT5 ticket map |
| `trade_bot.log` | Runtime log (git-ignored) |
| `*.session` | Telethon login cache (git-ignored) |

---

## ⚠️ Disclaimer

This software places **real trades with real money**. Trading forex/CFDs carries
significant risk of loss. Test on a **demo account** first, understand the code,
and use at your own risk. The author accepts no liability for any financial loss.
