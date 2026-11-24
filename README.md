# kraken-execution-engine

An automated execution engine that reads trade signals from a Telegram channel and
trades them on **Kraken perpetual futures**, built with a safety-first, fail-closed design.

> **Status:** complete, ran 24/7 in paper mode on Linux. Retired after a backtest showed
> the signal source itself was unprofitable. The engineering is the point of this repo,
> not the strategy. Nothing here is financial advice.

## Highlights

- **756 automated tests** (pytest), including a custom static-analysis audit that fails
  the build if any safety mechanism is implemented but never called in production.
- **Soak test passed 85/85 checks**, max protection latency **0.84 s** against a 15 s limit.
- **Crash-safe:** on restart the bot reconciles its local state against the exchange
  before it is allowed to open anything new.
- **Found a live-only bug:** Kraken's `set-leverage` re-margins an open position in live
  trading even though paper trading reported a no-op. Fixed at the root with a
  margin-headroom guard in recovery and reconciliation.
- **Runs as a service:** systemd unit on Linux (WSL2), auto-start after reboot,
  network-ready gating, real-time Telegram alerts and a daily P&L summary.

## How it works

```
Telegram message
   -> parser      (normalise -> classify -> extract; precision over recall)
   -> admission   (whitelist, dedupe, symbol occupancy, stale-price guard)
   -> sizing      (fixed margin, instrument specs read live, Decimal maths)
   -> execution   (entry, stop-loss, TP ladder, breakeven, confirmed close)
   -> reconcile   (exchange is the source of truth; mismatches quarantined)
```

The run loop has three cadences: **protection every 2 s**, strategy every 15 s,
reconciliation every 60 s. A kill switch blocks new entries but never blocks
protective orders or exits.

| Component | Path | Role |
|---|---|---|
| Config | `src/bot/config.py` | Fail-fast validation; live trading needs a second, independent opt-in |
| Domain model | `src/bot/models.py` | Trade state machine with legal transitions only |
| Persistence | `src/bot/db/` | SQLite; intent and actual state kept separate; append-only event log |
| Parser | `src/bot/parser/` | Turns free-text signals into typed instructions |
| Admission | `src/bot/admission.py` | Decides whether a parsed signal may trade |
| Sizing | `src/bot/sizing.py` | Position size from margin, leverage cap and instrument specs |
| Exchange | `src/bot/exchange/` | `ExchangeClient` interface over the official Kraken CLI (`-o json`), live/paper routing |
| Execution | `src/bot/execution/` | Order lifecycle, fills observed from the exchange, never assumed |
| Recovery | `src/bot/recovery.py`, `src/bot/reconcile.py` | Crash-window resolution and drift repair |
| Telegram | `src/bot/telegram/listener.py` | Idempotent listener with backfill after downtime |
| Alerts | `src/bot/alerting.py` | Per-transition and per-action alerts, daily summary |

## Safety design

- **Hard 10x leverage cap**, enforced regardless of what a signal says.
- **Stale-signal guard:** refuses entries if price has drifted too far from the signal
  (1.5% default; 3.5% for tokenised gold, which tracks spot gold less tightly).
- **Unknown positions are quarantined**, never touched or closed automatically.
- **Fail closed:** anything that cannot be verified is treated as a failure, in the bot
  and in the test tooling.
- **Wiring audit:** `tests/test_wiring.py` and `tools/audit_excuses.py` walk the call
  graph (AST) so a safety check that exists only in unit tests cannot ship.
- **Secrets** live only in `.env` (see `.env.example`); logs redact them in two layers.

## Run it

Requires Python 3.12 and [kraken-cli](https://github.com/krakenfx/kraken-cli).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in your own keys; TRADING_MODE=paper by default
pytest                        # full test suite
python -m bot.main --status   # read-only status
```

Paper mode is the default. Live mode requires `TRADING_MODE=live` **and** a separate
acknowledgement variable, by design.

## Docs

- `docs/PLAN.md` — build plan and stage gates
- `docs/KRAKEN_NOTES.md` — exchange behaviour measured from the live API
- `docs/STAGE10_SOAK.md` — soak test design and results
- `docs/FINAL_REPORT.md` — final audit

## Built with

Python 3.12, Telethon, SQLite, pytest, systemd. Developed with Claude Code as an
AI pair programmer.

## Disclaimer

Educational project. Trading leveraged derivatives can lose more than your margin.
No signal source is included or endorsed.
