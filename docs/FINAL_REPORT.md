# Final report — Telegram → Kraken Pro signal execution bot

Date: 2026-07-30 · 649 tests passing · Stage 10 soak PASS (run 6)

**This is not ready for live trading.** Five verification items are blocked on
Kraken credentials that do not exist on this machine. See §11.

---

## 1. What was built

| Component | File | Role |
|---|---|---|
| Config | `config.py` | Fail-fast validation; live needs a second independent variable |
| Domain types | `models.py` | Enums + the trade state machine with legal transitions |
| Persistence | `db/store.py`, `db/schema.sql` | SQLite; intent and actual kept separate; append-only events |
| Logging | `logging_setup.py` | Structured JSON, two-layer secret redaction |
| Kill switch | `killswitch.py` | Blocks opening; never blocks protection or exits |
| Parser | `parser/` | Normalise → classify → extract, precision over recall |
| Admission | `admission.py` | Spot boundary, whitelist, dedupe, occupancy, live-position check |
| Sizing | `sizing.py` | Instrument-spec-driven; Decimal throughout |
| Exchange | `exchange/` | `ExchangeClient` interface over kraken-cli; live/paper routing |
| Execution | `execution/engine.py` | Entry, stop, TP ladder, breakeven, close-with-confirmation |
| Lifecycle driver | `execution/manager.py` | Forward path + O(1) protection sweep |
| Fill observation | `execution/observer.py` | Confirmed fills only, never submission responses |
| Reconciliation | `reconcile.py` | Consistent snapshot, mismatch detection and repair |
| Recovery | `recovery.py` | Crash-window resolution and the no-trade-before-recovery gate |
| Run loop | `runner.py` | Three cadences: protect 2s, strategy 15s, reconcile 60s |
| Pipeline | `pipeline.py` | Message → parse → admit → open/close |
| Telegram | `telegram/listener.py` | User-account client, idempotent, backfills after downtime |

Tooling: `tools/test_inject.py` (signal injection), `tools/soak_run.py` +
`soak_bot.py` (soak harness), `tools/measure_tick.py`, `tools/audit_excuses.py`.

## 2. Reused vs replaced

**Nothing was reused.** The repository was empty at Stage 1 and a search of the
home directory found no prior OKX bot, `test_inject.py`, `.env`, Telegram
session or database. The "migrate vs. replace" decision resolved to a clean
build by necessity, not preference. No OKX code could leak in because none was
present; the brief's OKX detail was treated purely as a description of bugs to
avoid.

**Replaced by design:** every exchange assumption. Symbol format, contract
semantics, quantity model, order types, stop semantics, leverage model and
position model were all read from Kraken's live API rather than carried over.

## 3. How the Kraken integration works

**Product:** `PF_` flexible perpetual futures (`type: flexible_futures`).

**Access:** kraken-cli 0.3.2 as a subprocess with `-o json`, behind one gateway
(`exchange/cli.py`) that owns timeouts, error-envelope classification, retry
policy and secret redaction. MCP was rejected — it exists to expose tools to an
LLM in the loop, whereas this executor is deterministic code, and the subprocess
boundary is what makes the whole engine testable without a network.

**Instrument specs are read, never hardcoded:**

| Canonical | Symbol | Size incr. | Tick | Max lev |
|---|---|---|---|---|
| BTC | `PF_XBTUSD` | 0.0001 | 1 | 100× |
| ETH | `PF_ETHUSD` | 0.001 | 0.1 | 100× |
| SOL | `PF_SOLUSD` | 0.01 | 0.01 | 50× |

Size is denominated in **base-asset units**, `contractSize = 1`. So
`$300 notional ≠ 300 contracts` — the brief's forbidden assumption, confirmed
false by a paper order that filled at exactly 0.0046 BTC. Size increment is
`10^-contractValueTradePrecision`; minimum order size equals one increment
(doc-verified, `KRAKEN_NOTES.md` §4.1.1). Max leverage is
`1 / retailMarginLevels[0].initialMargin`, read dynamically even though the 10×
cap always binds.

**Leverage is per-symbol account state, not an order parameter.** Live orders
take no `--leverage` flag. It is set via `set-leverage` and **verified by
reading back**, because Study B proved a preference persists after a failed
order and then governs the next position on that symbol.

## 4. How sizing works — worked example

`TRADE_MARGIN_USD=30`, `MAX_LEVERAGE=10`, BTC at 64,598, signal says **50×**:

```
effective leverage = min(50, 10, 100)          = 10×      ← capped
total notional     = $30 × 10                  = $300
entry 1 margin     = $15  → $150 notional
entry 2 margin     = $15  → $150 notional

entry 1 size = 150 / 64,000 = 0.00234375 → round DOWN to 0.0023
entry 2 size = 150 / 63,500 = 0.00236220 → round DOWN to 0.0023
total position                              = 0.0046 BTC
```

Rounding is always **down**, so rounding can never overshoot the intended
margin. Every result is validated against the live spec — below minimum,
off-increment, or a material deviation from intended notional all raise rather
than submit. That matters because **paper does not enforce increments** (it
filled 0.000001 BTC against a 0.0001 increment), so the exchange is not a
safety net.

## 5. Entry orders

Margin is split 50/50, **not contracts** — the two entries sit at different
prices, so equal margin buys different sizes. Both are limit orders with a
24-hour lifetime; the expiry sweep cancels stragglers.

Before anything is placed: the stale-signal guard compares mark against the
entry range and refuses beyond `MAX_ENTRY_DEVIATION_PERCENT`, margin
availability is checked, and leverage is set and verified.

## 6. TP / SL / breakeven

**Stop:** sized from the **actual** position, never the intended one. If only
one entry fills, the stop covers exactly that. Reduce-only, mark-triggered, and
tick-rounded *away* from entry so snapping can never tighten it.

**Protection SLA:** a position must reach protected within **15 s** of its
fill, by any path. `protection_sweep()` makes exactly two exchange reads
regardless of book size and runs every 2 s. Measured in the soak: **0.56 s and
0.84 s**. (A full strategy step costs O(open orders) — 17 s median, 50 s worst
with three trades — which is why protection cannot ride on it.)

**TP ladder:** 25% each, remainder to the earliest levels, final level absorbs
residue so the parts sum to **exactly** the position — never more, which could
open a reverse position. All reduce-only.

**Breakeven:** authorised **only** by a confirmed TP2 *fill* — not price
touching TP2, not the order existing. Enforced twice: the state table makes
`BREAKEVEN_ACTIVE` reachable only from `TP2_FILLED`, and the engine re-checks
before touching the exchange. The move is a single atomic `edit-order` amend
that preserves the order id, so there is never a window with two stops or none
— the historical duplicate-stop bug is structurally impossible, not merely
avoided. Breakeven uses the **actual weighted-average fill price**, so one
filled entry gives that entry's price, not a blend.

## 7. How close confirmation works

```
close instruction → resolve target (reply → symbol → direction → explicit all)
                  → undeterminable? REFUSE, never guess
close order submitted           → NOT closed
await exchange-confirmed flat   → timeout means NOT closed
cancel every remaining order    → entries included, or they could reopen
verify none remain              → leftovers → SAFE_ERROR, not CLOSED
                                → only now CLOSED
```

`CLOSED` is reachable from `CLOSING` alone — asserted by a test that enumerates
the whole transition table. A close that reports success while exposure remains
logs at ERROR and lists what is still open, so "close applied" can never be read
as "everything is flat".

Targeting uses a **deny-list** (closeable = not terminal). An allow-list of
"good" states previously omitted `REQUIRES_REVIEW`, and "close all" reported
*nothing to close* while a quarantined trade held 0.0046 BTC.

## 8. How restart recovery works

Local state after a crash is only as current as the last completed write, so
recovery rebuilds from the exchange and treats the database as a record of
*intent*.

Order intent is written **before** submission with a deterministic client order
id, which makes the dangerous window recoverable:

| Snapshot says | Outcome |
|---|---|
| resting | adopt the exchange id |
| in fills | record the fill |
| absent, symbol flat | never placed — safe to abandon |
| **absent, position exists** | **AMBIGUOUS → REQUIRES_REVIEW** |

The last is never guessed: fills history is bounded, so absence is not proof.

**The gate:** `_recovery_complete` starts False on every process and only
`RecoveryManager` clears it. Recovery that cannot complete leaves it shut and
startup exits non-zero.

Quarantine suspends **strategy, not protection** — a `REQUIRES_REVIEW` trade
keeps its stop and remains closeable by an operator. An ambiguous trade
quarantines itself only; other symbols keep trading.

Verified against paper: a process killed mid-flight with unsynced fills, no
stop and leaked 3× leverage came back with both fills adopted, protection
restored at the right size, leverage back to 10×, and **zero duplicate orders**.

## 9. How to run it

```bash
git clone <repo> && cd kraken-signal-bot
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # then edit

.venv/bin/python -m bot.main --check     # startup validation, exits
```

`--check` runs the full startup path: config validation, kill-switch state,
API-key permission checks, instrument resolution and restart recovery.

### Tests

```bash
.venv/bin/python -m pytest -q                      # all 649
.venv/bin/python -m pytest -m unverifiable_in_paper  # assertions paper cannot make
.venv/bin/python tools/audit_excuses.py            # wiring-registry audit
.venv/bin/python tools/soak_run.py                 # 30-minute paper soak
```

### Test injection

```bash
# Parse + size only, never touches the exchange
.venv/bin/python tools/test_inject.py --dry-run "BTC LONG
Entry: 64000 - 63500
TP1: 65000
TP2: 66000
TP3: 67000
TP4: 68000
SL: 62500
Leverage: 50x"

# Through the real pipeline (respects TRADING_MODE)
.venv/bin/python tools/test_inject.py "Close BTC"
.venv/bin/python tools/test_inject.py --file signals.txt
```

## 10. Paper vs live

**Paper (default, no credentials needed):**

```bash
TRADING_MODE=paper
```

**Live — two independent variables, deliberately:**

```bash
TRADING_MODE=live
LIVE_TRADING_ACK=I_UNDERSTAND_THIS_IS_REAL_MONEY
KRAKEN_FUTURES_API_KEY=...
KRAKEN_FUTURES_API_SECRET=...
KRAKEN_FUTURES_WITHDRAWAL_ATTESTATION=WITHDRAWAL_API_SET_TO_NO_ACCESS
```

`TRADING_MODE=live` alone **refuses to start**, so no single forgotten variable
can send real orders. The key must be created with **General = Full Access,
Withdrawal API = No Access**. Every startup logs `TRADING MODE: LIVE`.

Kill switch: `touch ./KILL_SWITCH` stops new positions; existing positions stay
managed and closes still work.

## 11. Known limitations — honest

### Blocked on live credentials (must be done before live)

1. **API-key withdrawal tests A–D** — the check is doc-verified, not
   API-verified (`KRAKEN_NOTES.md` §1.6).
2. **Live `open-orders` returning `stopPrice`** — paper omits it, so stop
   trigger-price verification is unproven live (§4.2).
3. ~~Study B on live~~ — **resolved 2026-07-30, with a real finding.** B(a)
   was live-tested against a real position and diverges from paper: paper
   showed set-leverage as a no-op on an open position, live showed it
   measurably increases the account's initialMargin and cuts available
   margin, even though it leaves the position's size/price and
   maintenanceMargin alone. Both re-assertion call sites (recovery.py,
   reconcile.py) are now margin-gated as a result — see §1C. B(b) stays
   paper-verified only by deliberate operator decision, not a blocker: the
   code's read-verify-mutate-verify pattern doesn't depend on it.
4. **Minimum order size** — doc-verified as one increment; never seen rejected.
5. **WebSocket fill stream** — auth-only, no paper equivalent, so it is
   unverified. Mitigated by design: it only *wakes* the reconciler and never
   mutates state, so a stream bug costs latency, not correctness.

### Functional gaps

6. **`SL_UPDATE`, `ENTRY_UPDATE`, `ADD_POSITION` are parsed and recorded but
   not executed** — they land in `REQUIRES_REVIEW`. Brief 19/20 asks for
   deterministic resolution; the parsing and target-resolution exist, the
   mutation path does not.
7. **GOLD/XAU is disabled.** Kraken lists no spot-gold perpetual. `PF_XAUTUSD`
   (Tether Gold) failed its tracking study — p99 basis 0.836% and a 1.417%
   intra-hour swing against spot gold, which would consume 94% of the 1.5%
   stale-entry budget with gold not moving. `ALLOW_XAUT_FOR_GOLD` is pinned
   `False` in code.
8. **One active trade per symbol.** Kraken nets positions per symbol, so two
   concurrent trades on one symbol cannot be protected independently. A second
   signal is recorded `REQUIRES_REVIEW`. Concurrency across symbols is
   unlimited.
9. **No dashboard.** Deliberate — structured JSON logs, the SQLite database and
   `tools/test_inject.py` are the observability surface. ~~The two states an
   operator most needs to know about immediately~~ — **resolved.**
   `REQUIRES_REVIEW` and `SAFE_ERROR` now push a real-time Telegram alert the
   moment a trade actually transitions into either (`src/bot/alerting.py`,
   wired at the two places those transitions happen: `_enter_safe_error` in
   `execution/engine.py` and the crash-window loop in `recovery.py`). Uses a
   separate send-only Bot API credential (`ALERT_TELEGRAM_BOT_TOKEN`/
   `ALERT_TELEGRAM_CHAT_ID`), distinct from the Telethon user-account listener
   that reads the signal channel. Best-effort by design — a failed send is
   logged and swallowed, never allowed to block or crash the transition it's
   reporting on (covered by dedicated failure-injection tests). Run
   `python -m bot.main --test-alert` to confirm the credentials work
   standalone, without running startup or touching the exchange.
   **Verified end-to-end 2026-07-30:** a real alert was sent via
   `--test-alert` and confirmed received by the operator on Telegram from
   `@example_bot` — so this path is live-tested, not just
   unit-tested. Note that a send is only reported successful if Telegram's
   response body carries `ok: true`; HTTP 200 alone is not treated as
   delivery, for the same reason a submitted order is never treated as a
   filled one (brief 27).

   **Daily digest (added 2026-07-30).** The per-transition alert fires once,
   at the moment a trade enters either state, and then nothing reminds anyone
   it is still sitting there. `send_unresolved_digest` closes that: at most
   once every 24 hours it sends one summary listing every unresolved trade
   with its id, symbol, state, how long it has been stuck, and the original
   reason:

   ```
   2 trade(s) awaiting review:

   • trade 1 (PF_XBTUSD) — REQUIRES_REVIEW
     stuck ~3d 5h | unresolvable order intent after restart

   • trade 2 (PF_ETHUSD) — SAFE_ERROR
     stuck ~3d 5h | UNPROTECTED: stop resize failed, position exceeds stop size
   ```

   It reuses `send_alert` — no second transport — and is driven by
   `BotRunner.tick` on a 300s *check* cadence, while the real 24h interval is
   enforced by a persisted `ALERT_DIGEST` event row rather than an in-process
   timer, because `time.monotonic()` resets on restart and a daily-restarted
   bot would otherwise never reach 24h. **Silent when nothing is
   unresolved** — a channel that pings daily to say "all fine" trains you to
   ignore it. A *failed* send deliberately does not consume the window, so
   one transient blip cannot cost a day of visibility.

   Reporting the age accurately required a real fix: `TRADE_STATE_CHANGED`
   existed in the event vocabulary but was never written by anything, and
   `trades.updated_at` is bumped by `update_trade_actuals` too, so it means
   "last touched", not "state last changed" — a long-quarantined trade would
   have reported as freshly changed. `transition_trade` now writes that event
   inside the same transaction as the state change, so the audit trail cannot
   disagree with the state it describes.

   **Verified end-to-end 2026-07-30**, same as the per-transition alert: real
   digests were sent from a scratch database to the operator's Telegram and
   confirmed received, with correct formatting and ages. The live run also
   caught a bug the test suite could not: `now` is injectable, but the
   `ALERT_DIGEST` marker was stamped with the store's clock, so a caller
   passing `now` got duplicate sends. Two tests were green over it — one used
   the default clock on both calls, the other advanced the clock and expected
   a re-send, which is indistinguishable from the bug. Fixed by giving
   `append_event` an `occurred_at` override; re-verified live afterwards
   (send, suppressed re-send, then a correct re-send at +25h). Worth
   recording as a pattern: this is the fourth time in this project that
   exercising a path for real found something green tests had missed, after
   the three invalidated soak runs.

   Everything else about observability here still holds: no dashboard,
   logs/DB/test_inject remain the surface for anything short of these two
   states.

### Known behaviours worth knowing

10. **Paper does not enforce size increments** and omits `stop_price`, so paper
    cannot validate rounding or trigger-price verification.
11. **Orphan positions are never auto-closed**, only reported — loudly on first
    sight, then every 5th cycle, with a standing unresolved-findings count.
12. **A trade opened before this build's admission checks** (i.e. a manual
    position) blocks new signals on that symbol until resolved.

## 12. What the soak does and does not prove

Run 6 passed all criteria with 85/85 checks verified, 0 violations, and
protection latency 20× inside its SLA. That establishes internal consistency
over 30 minutes including a real `SIGKILL`.

It establishes nothing about live execution, real fill dynamics, slippage,
funding, or the five credential-blocked items above. Six runs were needed: two
found real bugs, three were invalidated by defects in the *harness* — a checker
that swallowed its own failures, a zombie process polluting the shared account,
and an SLA check that silently took zero samples. Each was the same mistake the
bot was audited for: treating *could not verify* as *verified fine*. That the
harness had to learn it three times is itself worth recording.
