# Stage 1 — Findings & Build Plan

Status: awaiting approval before Stage 2.
Date: 2026-07-29. kraken-cli v0.3.2.

---

## 1. Environment

| Item | Value |
|---|---|
| OS | macOS 26.6, Darwin 25.6.0, arm64 (Apple Silicon) |
| Python | 3.14.6 (`/opt/homebrew/bin/python3`), pip 26.1.2 |
| Node | v26.5.0 |
| kraken-cli | 0.3.2, `~/.cargo/bin/kraken`, on PATH ✅ |
| Repo | `~/projects/kraken-signal-bot`, git initialised, **zero commits, no files** |

### Prior implementation: none on this machine

Searched the home directory for any OKX bot, `test_inject.py`, signal-bot project,
existing `.env`, Telegram session, or SQLite database. **Nothing found.** The only
directory is this empty repo.

So brief §1's "migrate vs. replace" decision resolves to **clean build**, not by
preference but because there is nothing to migrate. No OKX code can leak in, since no
OKX code is present. All historical OKX detail in the brief is treated as design
context describing bugs to avoid, never as code to port.

**Nothing is reused. Everything is new.**

---

## 2. What kraken-cli gives us for free

Verified by reading the repo docs (AGENTS.md, CONTEXT.md, skills/INDEX.md) and by
executing the CLI locally. Local execution is authoritative for v0.3.2.

### Invocation contract
- `kraken <cmd> ... -o json` — stdout is always valid JSON, exit 0 = success.
- Errors are a stable envelope: `{"error":"<category>","message":"..."}`.
- 9 error categories with defined retry semantics. Route on `error`, never on message
  text. `network`/`rate_limit`/`websocket` retryable; `auth`/`validation`/`api` not.

### Covers essentially our whole exchange layer
- `futures instruments` — full instrument specs (public, no auth)
- `futures ticker` — mark / index / last / bid / ask
- `futures order buy|sell` — 8 order types incl. `stop`, `take-profit`
- `futures edit-order` — **atomic amend** of size / price / stop-price
- `futures cancel`, `cancel-all`, `cancel-after` (dead-man's switch)
- `futures open-orders`, `order-status`, `positions`, `fills`, `accounts`
- `futures leverage`, `set-leverage`
- `futures ws fills | open-orders | open-positions` — private event streams
- `futures paper ...` — full simulated engine, live prices, no API keys

**We write no Kraken HTTP client, no request signing, no key handling, no rate-limit
plumbing.** No raw-API fallback is needed for anything in the brief.

### Order flags we depend on (verified present on `futures order buy/sell`)
```
--type {limit,post,market,stop,take-profit,ioc,trailing-stop,fok}
--price --stop-price --trigger-signal {mark,index,last}
--reduce-only --client-order-id
```
`--client-order-id` and `--reduce-only` are exactly the two primitives the brief's
idempotency (§30) and TP-safety (§13) requirements need. Both confirmed working.

---

## 3. Kraken's actual product model (verified, not assumed)

Product: **`PF_` flexible perpetual futures**, `type: flexible_futures`.

### Live specs pulled from `kraken futures instruments`

| Canonical | Kraken symbol | contractSize | Size incr. | tickSize | Max lev | Mark (probe) |
|---|---|---|---|---|---|---|
| BTC | `PF_XBTUSD` | 1 | 0.0001 | 1 | 100x | 64,601.70 |
| ETH | `PF_ETHUSD` | 1 | 0.001 | 0.1 | 100x | 1,917.93 |
| SOL | `PF_SOLUSD` | 1 | 0.01 | 0.01 | 50x | 73.97 |
| XAU | `PF_XAUTUSD` ⚠️ | 1 | 0.001 | 0.1 | 50x | 4,019.75 |

⚠️ `PF_XAUTUSD` is Tether Gold, **not** spot XAU/USD — enabled by explicit operator
decision. See §4.4.

Note BTC is `XBT` in Kraken's symbology, not `BTC`.

### The quantity model — confirms the brief's warning was right to raise

`contractSize: 1` and size is denominated in **base-asset units**, not contracts and
not USD. `kraken futures paper buy PF_XBTUSD 0.0046` produced a position of exactly
`0.0046 BTC`. So:

```
size_base = notional_usd / entry_price
```

**`$300 notional ≠ 300 contracts`** — the brief's forbidden assumption. Confirmed
false here. And `quantity = notional / price` *is* correct for PF_ contracts, but only
because `contractSize == 1`; the sizing engine will still read `contractSize` from the
API rather than hardcode 1, so an instrument with a different contract size cannot
silently mis-size.

- **Size increment** = `10^-contractValueTradePrecision` (BTC 4 → 0.0001).
- **Price increment** = `tickSize`.
- **No explicit minimum order size field.** Effective minimum = one size increment.
  To be confirmed against a live rejection at Stage 4; until confirmed the sizing
  engine treats "≥ 1 increment" as the floor and additionally requires the rounded
  size to stay within a configurable tolerance of intended notional.
- **Max leverage** = `1 / retailMarginLevels[0].initialMargin`. All three are ≥ 50x,
  so our 10x cap always binds — but it is read dynamically, never assumed.
- Margin is **tiered by position size** (`retailMarginLevels`). Irrelevant at $30
  margin, but the adapter will expose the schedule rather than flatten it.

---

## 4. Four findings that change the design

### 4.1 🔴 Positions are NETTED per symbol — one position per symbol, no hedge mode

Verified: a second `PF_XBTUSD` buy merged into the existing position at a
weighted-average entry price. `positions` returns **one row per symbol** with a single
`side`. There are no position IDs and no hedge mode.

This structurally conflicts with brief §24 ("unlimited concurrent positions"). Two
concurrent BTC LONG signals cannot be two independent positions with independent
SL/TP ladders — the exchange will merge them, and per-trade stop quantities would then
fight each other over one shared position.

Per the brief's own tiebreaker ("Kraken's verified behavior wins", "safety wins"):

> **Rule: one active trade per symbol.** A new signal for a symbol that already has an
> active trade is recorded, classified `REQUIRES_REVIEW`, and **not executed**.

Unlimited concurrency *across* symbols is unaffected. This is not an artificial
position cap; it is the exchange's actual position model. Flagged explicitly because
it is a visible behavioural difference from the brief.

### 4.2 🟢 `edit-order` is an atomic amend — this kills the duplicate-stop bug (§16)

Verified: amending a resting stop's trigger price and size returned
`{"action":"order_edited","order_id":"FP-00003"}` — **same order ID, same client order
ID, open-order count unchanged at 2.** No cancel/replace window. There is never a
moment with two stops or zero stops.

During the probe the amended stop then actually *fired* at its new trigger and new
size, confirming the amendment is live rather than cosmetic.

So the breakeven transition (§15) becomes a single atomic amend, not the dangerous
cancel-then-place sequence that caused the historical bug. Cancel-then-place is kept
only as a fallback if `edit-order` returns an error, gated behind verification, with
failure driving the trade to a `SAFE_ERROR` state.

### 4.3 🟠 Live leverage is account state, not an order parameter

`kraken futures order buy` has **no `--leverage` flag`**. Leverage is a per-symbol
account preference: `kraken futures set-leverage PF_XBTUSD 10`. Placing an order with
no preference set fails with a `validation` error (reproduced).

Confusingly, **`futures paper buy` *does* accept `--leverage`** per order. The adapter
must hide this asymmetry, and — more importantly — leverage becomes a **startup safety
check**: assert the on-account leverage preference for every whitelisted symbol equals
the configured cap before the execution engine is allowed to run.

#### 4.3.1 Leverage is mutable shared state — a state-leak risk

Because leverage is per-symbol *account* state rather than an order parameter, a
`set-leverage` call mutates something that outlives the order it was made for. Two
behaviours must be established empirically at Stage 4 before the execution engine can
depend on them — assuming either way would be guessing:

**(a) Does `set-leverage` affect an already-open position, or only subsequent orders?**
If it retroactively re-margins an open position, then changing leverage for a new
signal silently alters the margin and **liquidation price of an unrelated live
position** on the same symbol. That would make `set-leverage` unsafe to call whenever
any position is open on that symbol, and would force the engine to set leverage only
on a flat symbol.

**(b) If `set-leverage` succeeds but the subsequent order call fails, does the symbol
retain the changed leverage?** Almost certainly yes — they are independent calls with
no transaction around them. That leaves the account in a state the local DB never
recorded an intent for, and the *next* signal on that symbol would execute under a
leverage value set for a trade that never happened.

Both are reconciliation concerns, not just execution concerns: leverage is exchange
state that can drift from local intent exactly like orders and positions can. So:

- Stage 4 measures both behaviours against the live account and records the answers in
  `docs/KRAKEN_NOTES.md`.
- `set-leverage` is wrapped in the adapter as an explicit state mutation: read current
  preference → compare → mutate only if different → **verify by reading back**.
- The reconciliation loop (Stage 7) adds leverage preference to the set of state it
  compares local-vs-exchange, and flags any symbol whose on-account leverage differs
  from configured intent.
- Recovery (Stage 8) re-asserts leverage preferences for all whitelisted symbols before
  the engine is allowed to place orders, since a crash between `set-leverage` and the
  order is precisely case (b).

### 4.4 🔴 GOLD/XAU does not exist as a Kraken perpetual — needs your decision

Exhaustive scan of all 294 instruments (including expired and untradeable):

- **No `PF_XAUUSD`.** No instrument anywhere with base `XAU` or name containing GOLD.
- `PF_XAGUSD` exists — that is **silver**.
- `PF_XAUTUSD` exists — that is **XAUT (Tether Gold)**, base asset `XAUT`, and Kraken
  categorises it `"Stablecoin"`. It is a gold-backed *crypto token* perp, carrying
  Tether issuer/redemption and depeg risk, its own basis, and thinner liquidity
  (open interest 710 vs BTC's 1,919). It is **not** spot XAU/USD.

> **RESOLVED 2026-07-29 — Study A ran and FAILED. GOLD stays unsupported.**
> Measured p99 basis 0.836% (tolerance 0.5%) and max intra-hour basis change
> 1.417% (tolerance 0.25%), robust to both the GC=F proxy premium and session
> boundaries. A single observed 1.417% basis move would consume 94% of the
> entire 1.5% stale-entry budget with gold not moving at all. Full method,
> data and anti-artifact checks in `docs/KRAKEN_NOTES.md` §1B. The pin stays.

**DECISION (operator, 2026-07-29): trade `PF_XAUTUSD` for GOLD/XAU — but NOT in v1.**

I raised counterparty/depeg risk; the operator affirmed the substitution and then
identified a second, distinct risk that gating does not address (see 4.4.1). Net
result: the mapping is built, but **disabled at the code level for v1**.

#### 4.4.1 Tracking-error risk — the reason v1 ships with this off

Depeg risk and *tracking* risk are different failure modes. The Telegram channel
writes GOLD entry / TP / SL levels against **spot XAU/USD**. We would execute against
**XAUT perp** prices. Those two series can drift apart under entirely normal
thin-liquidity conditions — `PF_XAUTUSD` carries ~711 open interest versus BTC's
1,919 — with no depeg event required.

That drift is not merely a P&L haircut; it corrupts price-derived control logic:

- **Stale-signal guard (§12)** compares live price against the signal's entry range.
  Comparing an XAUT perp mark against a spot-gold entry range makes
  `MAX_ENTRY_DEVIATION_PERCENT` measure basis, not staleness — so it can reject valid
  signals and, worse, admit genuinely stale ones.
- **TP and SL trigger levels** would be spot-gold numbers applied to an XAUT mark. A
  stop set at a spot-derived level can sit at the wrong effective distance, or trigger
  on basis movement rather than on the move the signal was about.

Gating cannot fix this, because it is a units mismatch, not an access-control problem.
It has to be **measured** before the substitution can be trusted.

#### 4.4.2 v1 position

- `ALLOW_XAUT_FOR_GOLD` is **hardcoded `false` in v1**, independent of what the
  environment or `.env` says. An operator setting it `true` gets a startup warning that
  the flag is pinned off pending the Stage 4 tracking study, and gold stays unsupported.
- The pin is lifted only after (a) the Stage 4 tracking measurement passes its
  tolerance, and (b) BTC/ETH/SOL have run cleanly. Lifting it is a deliberate,
  reviewed change — not a config edit.
- Until then GOLD signals are parsed, stored, logged and marked
  `UNSUPPORTED_INSTRUMENT` — never silently dropped.

#### 4.4.3 When the pin is lifted

- Canonical `XAU` resolves to `PF_XAUTUSD` **only** through the flag; no hardcoded
  GOLD→XAUT path exists anywhere in the parser or adapter.
- Startup log states the substitution explicitly:
  `GOLD/XAU -> PF_XAUTUSD (Tether Gold, NOT spot XAU) [operator-enabled]`.
- Every gold trade record is tagged with the substitution, so the audit trail shows
  what was actually traded versus what the signal referenced.
- Per §38's last clause, an unavailable or untradeable `PF_XAUTUSD` must never block
  BTC/ETH/SOL startup.
- XAUT is otherwise treated like any other instrument: specs read live, sizing tested
  against its own increments (0.001, tick 0.1), leverage capped at 10x against its 50x
  maximum.

### 4.5 Paper-mode fidelity gaps (must not be mistaken for correctness)

- `futures paper orders` does **not** return `stop_price` (`price` is null on stop
  orders). So paper cannot fully verify a stop's trigger price — one leg of the §16
  verification is unverifiable in paper. Live `futures open-orders` is expected to
  return it; **to be confirmed at Stage 4 with read-only credentials.** Until
  confirmed, verification degrades to size + existence + type in paper, and the gap is
  logged loudly rather than papered over.
- No `--validate` (dry-run) flag on futures orders; it exists only on spot. Our own
  `dry-run` mode therefore stops at the adapter boundary and never invokes the CLI.

---

## 5. Architecture

### 5.1 Exchange layer: CLI subprocess, not MCP

**Recommendation: call `kraken` as a subprocess with `-o json`,** behind one gateway
module. Rationale:

- MCP exists to expose tools to an *LLM in the loop*. Our executor is deterministic
  code; the stdio protocol layer and the `acknowledged=true` / `--allow-dangerous`
  confirmation model add a moving part and buy nothing.
- We want *our* safety gating (kill switch, mode check, leverage assert) at the
  gateway, not the MCP server's.
- A subprocess boundary is trivially mockable — every adapter test injects recorded
  JSON, so the entire execution engine is testable with zero network.
- MCP v1 is REST-only and excludes the WebSocket groups we want for fills.

Every Kraken interaction funnels through one `KrakenCli.run()` that owns: `-o json`,
timeouts, exit codes, error-envelope classification, retry policy per category,
secret-free logging, and mode routing (`futures ...` vs `futures paper ...`). Exchange
specifics exist only below the `ExchangeClient` interface.

### 5.2 Component flow

```
Telegram Listener (Telethon, user account)
        │  persists raw message BEFORE any parsing
        ▼
Message Store (SQLite, UNIQUE(channel_id, message_id))
        ▼
Signal Parser  ──────────────► deterministic, no I/O, no network
        ▼
Signal Validator (whitelist, spot-guard, leverage cap, dedupe)
        ▼
Trade Intent  ── INTENDED state, persisted before any order
        ▼
Sizing Engine (live instrument spec → validated base-unit size)
        ▼
Execution Engine (trade state machine)
        ▼
ExchangeClient  ◄── the only Kraken-aware seam
        │
   ┌────┴─────────────────┐
   │ KrakenCliAdapter     │  live  → kraken futures ...
   │                      │  paper → kraken futures paper ...
   │                      │  dry   → no subprocess at all
   └────┬─────────────────┘
        ▼
Position/Order State (ACTUAL, exchange-sourced)
        ▲
Reconciliation Engine (60s safety net) + WS fill stream (fast path)
```

The **intent vs. actual** split from §39 is enforced structurally: intent columns and
actual columns are separate and never written by the same code path. Local DB is
authoritative for *intent and association*; the exchange is authoritative for *fills
and positions*.

### 5.3 Folder structure

```
kraken-signal-bot/
├── README.md
├── docs/
│   ├── PLAN.md                  ← this file
│   ├── PARSER.md                grammar, precedence, dedupe strategy
│   ├── RECOVERY.md              crash points & recovery matrix
│   └── KRAKEN_NOTES.md          verified API behaviour + probe transcripts
├── .env.example                 placeholders only, never real secrets
├── pyproject.toml
├── src/bot/
│   ├── config.py                env load + startup validation, fail-fast
│   ├── logging_setup.py         structured JSON logs, secret redaction
│   ├── models.py                enums + dataclasses (MessageType, TradeState…)
│   ├── db/
│   │   ├── schema.sql           messages, trades, orders, events
│   │   └── store.py             transactional repo layer
│   ├── telegram/listener.py     Telethon client, reconnect, backfill
│   ├── parser/
│   │   ├── normalize.py         emoji/unicode/obfuscation folding
│   │   ├── aliases.py           BTC/ETH/SOL/XAU alias table
│   │   ├── classify.py          message type classification
│   │   └── parse.py             field extraction → ParsedMessage
│   ├── validate.py              whitelist, spot-guard, leverage cap, dedupe
│   ├── sizing.py                instrument spec → validated size, TP split
│   ├── exchange/
│   │   ├── base.py              ExchangeClient interface
│   │   ├── cli.py               KrakenCli.run() gateway
│   │   ├── kraken.py            KrakenCliAdapter (live/paper)
│   │   ├── dryrun.py            in-memory simulator
│   │   └── specs.py             instrument spec cache + refresh
│   ├── execution/
│   │   ├── state.py             trade state machine + legal transitions
│   │   ├── entry.py             two-entry allocation, stale-signal guard
│   │   ├── tp.py                4-level ladder, remainder-to-earliest
│   │   ├── sl.py                stop lifecycle + atomic breakeven amend
│   │   └── close.py             close → confirm-flat → cancel → CLOSED
│   ├── reconcile.py             local vs exchange, repair rules
│   ├── recovery.py              startup reconstruction
│   ├── killswitch.py            file-based, checked before every new order
│   └── main.py                  wiring + supervisor loops
├── tools/
│   ├── test_inject.py           synthetic message injection + dry-run report
│   └── status.py                CLI status readout (no web UI)
└── tests/
    ├── fixtures/                recorded kraken-cli JSON + real message samples
    ├── test_parser_*.py
    ├── test_sizing.py
    ├── test_execution_*.py
    ├── test_reconcile.py
    ├── test_recovery.py
    └── test_regressions.py      the 10 named regression tests from §33
```

### 5.4 Safety mechanisms mapped to verified primitives

| Requirement | Mechanism | Verified |
|---|---|---|
| §16 no duplicate stop | `edit-order` atomic amend | ✅ probed |
| §30 order idempotency | `--client-order-id`, deterministic per (trade, purpose, seq) | ✅ flag exists |
| §13 TP never reverses | `--reduce-only` on every TP | ✅ probed |
| §14 stop tracks real size | stop size from exchange position, amended on each fill | ✅ amend works |
| §27 close confirmation | poll `positions` until flat, then cancel + verify, then CLOSED | ✅ endpoints exist |
| §25 kill switch | file presence checked before every order-opening call | design |
| §26 mode isolation | separate adapter routing; live requires explicit `TRADING_MODE=live` | design |
| §9 leverage cap | `set-leverage` asserted at startup per symbol | ✅ probed |
| fast fill detection | `futures ws fills` long-lived stream | ✅ exists |

---

## 6. Build plan

Each stage is independently tested and must be green before the next begins.

| Stage | Deliverable | Done when |
|---|---|---|
| **1** | Inspect & plan | ✅ this document |
| **2** | Skeleton: config, DB schema, structured logging, kill switch, enums/state machine, `.env.example`, **API-key permission check (§25)**. **First task: verify Telethon supports Python 3.14; if not, pin a 3.12/3.13 venv.** | Config fails fast on bad input; schema migrates; kill switch unit-tested; venv decision locked; **engine refuses to start if the key can withdraw or if permissions cannot be verified** |
| **3** | **Parser** — normalisation, aliases, classification, extraction. No network, no I/O. | All §33 parser tests pass, incl. spot-guard, `Cl0se`/`EX!T`, and the "booked"/"added" false-positive suite. Precision over recall. |
| **4** | **Kraken adapter** — `ExchangeClient` + CLI gateway + spec cache. Unit tests on recorded JSON; live read-only smoke test. Plus two studies: **XAUT-vs-spot-gold tracking (§4.4.1)** and **leverage state semantics (§4.3.1)**. | Instruments resolve; error envelopes classify correctly; **confirm live `open-orders` returns `stopPrice`** (§4.5); tracking tolerance measured and recorded; leverage questions (a) and (b) answered empirically and written to `KRAKEN_NOTES.md` |
| **5** | **Sizing** — margin+leverage+price+spec → validated size; TP ladder split with remainder-to-earliest. | Sizing tests pass for BTC/ETH/SOL/XAUT at multiple prices; rounding, min size, increment, leverage cap, insufficient margin, impossible order all rejected safely |
| **6** | **Execution** — state machine, two entries, stale-signal guard, TP ladder, SL lifecycle, atomic breakeven, close-confirm. | Full lifecycle tests against dry-run + paper; the "not CLOSED until flat" regression test passes |
| **7** | **Reconciliation** — 60s loop + WS fill fast path, mismatch detection and repair rules. | Injected divergences (missing/extra/partial/stale orders) detected and safely repaired |
| **8** | **Restart recovery** — crash at each dangerous point, reconstruct from exchange. | Crash-matrix tests pass: no duplicate orders, every open position ends protected |
| **9** | **Telegram** — Telethon listener, session persistence, reconnect, backfill, idempotent ingest. | Reconnect + restart produce no double-processing |
| **10** | **E2E paper** — `test_inject.py` driving full lifecycle in paper mode. | Signal → entries → TPs → breakeven → close, verified against paper positions |
| **11** | **Safety audit** — races, duplicate orders, rounding, stale stops, orphans, secret leakage, retry safety, parser false positives. | Audit written up; final report per §43 |

Stages 3 and 4 are independent and can overlap; 5 depends on 4; 6 depends on 3+4+5.

---

## 7. Operator decisions (settled 2026-07-29)

1. **GOLD/XAU → `PF_XAUTUSD`, built but pinned off for v1.** Operator elected to trade
   Tether Gold perps, then identified a tracking-error risk that gating cannot address.
   `ALLOW_XAUT_FOR_GOLD` is hardcoded `false` in v1; the pin lifts only after Stage 4
   Study A passes and BTC/ETH/SOL have run cleanly. See §4.4.
2. **One active trade per symbol.** Accepted. A second signal on an occupied symbol is
   recorded and flagged `REQUIRES_REVIEW`, never executed. Concurrency across distinct
   symbols remains unlimited. See §4.1.

**v1 tradeable set: BTC, ETH, SOL.** XAU(T) is implemented, tested and dormant.

Everything else I will decide and document as I go.

## 7a. Stage 4 study specs

### Study A — XAUT vs spot XAU tracking error

Gates whether the GOLD substitution may ever be enabled (§4.4.1).

- **Series 1:** `PF_XAUTUSD` mark price history, via `kraken futures history` / `ohlc`.
- **Series 2:** spot XAU/USD reference from a free public API, sampled over the same
  window. Two independent sources if available, to avoid trusting one feed.
- **Window:** at least 30 days of hourly samples, so the result reflects ordinary
  thin-liquidity behaviour rather than one quiet afternoon.
- **Metrics:** basis in percent per sample (`(xaut - spot) / spot`); report median,
  p95, p99, max absolute deviation, and the largest deviation *change* within any
  single hour — that last one is what actually threatens a stop.
- **Tolerance:** p99 absolute basis **< 0.5%**, and max intra-hour basis change
  **< 0.25%**. Rationale: `MAX_ENTRY_DEVIATION_PERCENT` and the TP/SL ladder operate at
  percent-scale distances, so basis noise must sit an order of magnitude below the
  control levels it would otherwise corrupt. If measurement suggests a different
  threshold is defensible, the number changes in this document with the evidence — not
  silently in code.
- **Outcome:** written to `docs/KRAKEN_NOTES.md` with the raw series retained under
  `tests/fixtures/`. Fail → GOLD stays unsupported and the pin stays. Pass → the pin
  may be lifted after BTC/ETH/SOL have run cleanly.

### Study B — leverage state semantics

Answers §4.3.1 (a) and (b) against the real account, in paper first where the
semantics are observable, then confirmed live read-only where safe.

- (a) Open a small position, call `set-leverage` to a different value, then re-read
  `positions` — did margin, liquidation price, or effective leverage on the **existing**
  position change?
- (b) Call `set-leverage`, then deliberately fail the following order (invalid size),
  then re-read `leverage` — did the preference persist despite the order failing?
- Both answers, with transcripts, go into `docs/KRAKEN_NOTES.md` and drive the
  reconciliation rules in Stage 7 and the recovery re-assertion in Stage 8.

## 8. Open items to verify in later stages

- Telethon on Python 3.14 (Stage 2 gate).
- ~~API key permission introspection~~ — **moved into Stage 2.** Whether withdrawal
  permission is detectable via the CLI, per §25. If it cannot be verified, the engine
  refuses to start (fail closed). Cheap to build and one of the highest-value checks in
  the brief, so it lands with the skeleton rather than waiting for the adapter.
- Live `open-orders` `stopPrice` presence (Stage 4, §4.5).
- Real minimum order size / min notional, via a deliberate live rejection (Stage 4).
- XAUT tracking error vs spot gold (Stage 4, Study A) — gates GOLD entirely.
- Leverage state semantics (Stage 4, Study B) — feeds reconciliation and recovery.

---

## 9. Unwired-safety-mechanism sweep (2026-07-29)

A systematic grep for functions whose name or docstring claims to enforce a
brief requirement, checking each for a call site reachable from the real
execution path — not from a test file or an integration script.

The same bug had already appeared twice by hand (`check_entry_freshness`,
`resize_stop_to_position`), so it was swept for mechanically rather than by
re-reasoning about each component.

### Found unwired

| Mechanism | Requirement | Severity |
|---|---|---|
| `effective_leverage` | brief 9 — **10x cap** | 🔴 critical |
| whole admission layer | brief 4/7/8/21/23 | 🔴 critical |
| `expire_stale_entries` | brief 12 — 24h entry expiry | 🟠 |
| `cancel_remaining_entries` | brief 11 | 🟢 redundant |

**The leverage cap was the worst.** `effective_leverage` had five passing unit
tests and zero production call sites. Leverage flowed caller → `plan_entries`
→ `set_leverage_preference` → exchange, uncapped, so a 50x signal would have
set 50x on the account. The single most important risk constraint in the brief
was unenforced.

**Five mechanisms had no caller because their layer did not exist.** The spot
boundary, whitelist, duplicate detection, one-trade-per-symbol and decision
recording all belonged to a signal→trade admission gate that was never built —
Stage 3 produced the parser, Stage 6 the engine, and nothing joined them. The
brief's hard safety line (§8, spot never becomes leveraged) had zero production
enforcement.

### Confirmed already wired

Kill switch (`open_trade`), insufficient-margin (`open_trade`), instrument
validation, TP-total-never-exceeds-position, stop verification, close
confirmation.

`cancel_remaining_entries` is genuinely redundant: `_finalise_close` cancels
every order purpose including entries. Proven by a test rather than assumed.

### Fixes

- `plan_entries` now takes `signal_leverage` and caps it internally; the
  parameter was renamed so an already-final value cannot be passed in. A second
  check before `set_leverage_preference` refuses a tampered plan.
- New `admission.py` — the missing gate, with every refusal carrying an
  enumerated reason.
- New `pipeline.py` — message → parse → persist → admit → create → open, so
  admission has a caller on the real path.
- Entry orders now carry `expires_at`; without it the expiry sweep had nothing
  to act on even once called.

### Durable fix

`tests/test_wiring.py` turns the sweep into a permanent test: every registered
safety mechanism must have a production call site, or the suite fails.
Mechanisms awaiting a future stage sit in `PENDING_WIRING` with a named owner,
and a second test fails if one becomes wired without leaving that list — so an
exemption cannot outlive its reason.

The guard was verified by deliberately removing the leverage-cap call site and
confirming it fails. A guard that cannot fail is the same trap it exists to
catch.

**Lesson:** a passing unit test proves a guard computes correctly. It says
nothing about whether the dangerous path invokes it. Both need asserting.
