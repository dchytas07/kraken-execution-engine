# Kraken verified behaviour notes

Everything here is either (a) executed against kraken-cli / the live API and
transcribed, or (b) read from Kraken's own documentation and explicitly
labelled as doc-verified-only. The two are never mixed silently.

---

## 1. API key permissions and the withdrawal check

Status: **DOC-VERIFIED, NOT API-VERIFIED.** Flagged for empirical test before
live trading — see §1.5.

### 1.1 Spot key: which permission gates the withdrawal endpoints

From Kraken's API key permissions guide, the **Withdraw Funds** permission
gates exactly:

> `WithdrawMethods, WithdrawAddresses, WithdrawInfo, Withdraw, WithdrawStatus,
> WithdrawCancel`

and **Query Funds** separately gates:

> `Balance, BalanceEx, TradeBalance`

Source: <https://docs.kraken.com/exchange/guides/rest/api-keys>

### 1.2 Correction: the endpoint docs require BOTH permissions

The per-endpoint reference pages are stricter than the guide above. Both
`GetWithdrawalMethods` and `GetWithdrawalAddresses` state, verbatim:

> "`Funds permissions - Query` and `Funds permissions - Withdraw`"

Sources:
- <https://docs.kraken.com/api/docs/rest-api/get-withdrawal-methods/>
- <https://docs.kraken.com/api/docs/rest-api/get-withdrawal-addresses/>

**This breaks the original assumption.** The Stage 2 check treated a *denial*
as proof that the key lacks Withdraw Funds. It is not. A denial has at least
two causes:

1. the key lacks **Withdraw Funds** (what we hoped to prove), or
2. the key lacks **Query Funds** but *does* have Withdraw Funds.

Case 2 is the dangerous one: a withdraw-capable key would be denied and scored
as safe. So the probe is sound in one direction only:

| Probe result | Inference | Sound? |
|---|---|---|
| **SUCCEEDS** | key definitively HAS Withdraw Funds (both perms required, so success implies both) | ✅ yes |
| **DENIED** | key lacks Query Funds, or Withdraw Funds, or both | ❌ proves nothing about withdrawal |

The check is therefore a **positive detector**: it can prove danger, it cannot
prove safety. The implementation now says so, and a denial is recorded as
`NOT_VERIFIABLE` rather than `PASS`.

### 1.3 Correction: the probe tests the wrong key entirely

`kraken withdrawal methods` authenticates with `KRAKEN_API_KEY` — the **Spot**
key. All trading in this bot happens on `PF_` perpetuals via
`KRAKEN_FUTURES_API_KEY` — a **separate credential with a separate permission
model**.

The Stage 2 check proved connectivity with the *futures* key and then drew a
conclusion from a *spot* key probe. That inference chain is invalid: the spot
probe says nothing whatsoever about what the futures key can do.

### 1.4 Kraken Futures key permission model

Futures keys use two independent permission axes:

**General API permissions**
- No Access — no endpoints
- Read-Only — read endpoints only
- Full Access — master key, "allows access to all endpoints, **excluding
  digital asset withdrawals**"

**Withdrawal API permissions** (a separate section)
- No Access — no access to digital asset withdrawals
- Full Access — allows digital asset withdrawals

Source:
<https://support.kraken.com/articles/360022839451-how-to-create-an-api-key-for-kraken-derivatives>

Two consequences:

1. **Good:** a futures key with General "Full Access" *cannot* withdraw.
   Withdrawal is an separate, deliberate grant — so the default trading key is
   withdrawal-incapable unless someone explicitly enabled it.
2. **Bad:** there is no read-only endpoint gated behind the futures Withdrawal
   API permission. The only way to exercise it is to attempt an actual
   withdrawal, which we will never do. **The futures key's withdrawal
   capability cannot be verified via the API at all.**

#### 1.4.1 Confirmed negative: no key-permission introspection endpoint exists

The conclusion above was originally inferred from withdrawal-specific docs. It
has since been checked directly against the whole API surface, rather than
assumed:

- **Full command catalog searched.** kraken-cli's `agents/tool-catalog.json`
  enumerates all **151** commands with parameter schemas. Searching the entire
  document for `permission`, `access level`, `accessLevel`, `key info`,
  `scope`, `privilege` and `introspect` returns **zero occurrences**.
- **Nearest candidates inspected and rejected.** `futures accounts` returns
  wallet balances and margin state, not key metadata. `futures notifications`
  returns account notices. `futures subaccount-status` returns a *subaccount's*
  trading-enabled flag, which is an account setting, not the authenticating
  key's permission set.
- **Docs agree.** Kraken's Futures API introduction lists capability categories
  with no key-introspection endpoint; its account endpoints are described as
  returning balances and PnL only.

So no "what can this key do?" endpoint exists on Kraken Futures, for withdrawal
scope or anything else. **The attestation approach stands, on a confirmed
negative rather than an inference.**

Source: <https://docs.kraken.com/api/docs/guides/futures-introduction/> and
kraken-cli `agents/tool-catalog.json` (151 commands, v0.3.2).

### 1.5 What the check does now, given the above

Brief 25 requires refusing to start if withdrawal permission is present *or
cannot be verified safely*. Read literally against 1.4(2), that would forbid
live trading permanently. The honest resolution is to separate what can be
machine-checked from what cannot:

| Credential | Capability | How it is handled |
|---|---|---|
| Spot key | withdrawal | Probed. Success ⇒ hard FAIL. Denial ⇒ `NOT_VERIFIABLE`, never `PASS`. Skipped entirely if no spot key is configured (it is not needed for trading). |
| Futures key | withdrawal | **Not probeable.** Requires an explicit operator attestation that the key's *Withdrawal API* permission is set to "No Access", recorded in config and echoed into the audit log. |

The attestation is not security theatre — it converts an unverifiable
assumption into a recorded, timestamped operator claim that appears in the
event log, so a post-incident review can see who asserted what and when.

### 1.6 OUTSTANDING — empirical verification required before live

Nothing in §1 has been executed against a real key; no credentials exist on
this machine yet. Before the first live run, this must be confirmed
empirically:

**Test A — withdraw-capable spot key is detected**
1. Create a Spot API key with **Query Funds + Withdraw Funds**.
2. Run `bot check` with it. Expected: `spot_withdrawal_scope = FAIL`,
   startup refused in live mode.

**Test B — restricted spot key is not falsely cleared**
1. Create a Spot API key with **Query Funds only** (no Withdraw Funds).
2. Run `bot check`. Expected: `NOT_VERIFIABLE` — *not* `PASS`. Confirms we do
   not read a denial as proof of safety.

**Test C — the ambiguity in 1.2 is real**
1. Create a Spot API key with **Withdraw Funds but NOT Query Funds**.
2. Call `kraken withdrawal methods`. If it is denied, that confirms a denial
   can mask a withdraw-capable key, validating the `NOT_VERIFIABLE` treatment.
   If it succeeds, the guide in 1.1 is right and the endpoint pages in 1.2 are
   over-stated — in which case a denial becomes meaningful and this section
   should be revised.

**Test D — futures trading key**
1. Create a Futures key: General = Full Access, Withdrawal API = **No Access**.
2. Confirm `kraken futures accounts` works and order placement works in paper.
3. Confirm the attestation gate behaves as documented.

Until Tests A–D are run, treat §1 as reasoned from documentation only.

---

## 1B. Study A — XAUT vs spot-gold tracking error

Status: **API-VERIFIED / MEASURED**, 2026-07-29. Verdict: **FAIL**.
Consequence: GOLD/XAU stays unsupported; the v1 pin stays in place.

### Method
- Series 1: Kraken `XAUTUSD` spot hourly close (the perp's underlying),
  721 points, 2026-06-29 10:00 → 2026-07-29 10:00 UTC.
- Series 2: COMEX front-month gold `GC=F` hourly close (Yahoo), 490 points.
- 490 overlapping hours. Basis = `(XAUT − gold) / gold × 100`.

### Result

| Metric | Measured | Tolerance | |
|---|---|---|---|
| median abs basis | 0.369% | — | |
| p95 abs basis | 0.665% | — | |
| **p99 abs basis** | **0.836%** | < 0.5% | ❌ FAIL |
| max abs basis | 1.736% | — | |
| median intra-hour change | 0.061% | — | |
| p99 intra-hour change | 0.318% | — | ❌ over |
| **max intra-hour change** | **1.417%** | < 0.25% | ❌ FAIL |

Basis stdev 0.217%; range −1.736% … +0.107%.

### Why this is not a measurement artifact

Three checks, because condemning an instrument on a proxy artifact would be as
bad as clearing it wrongly:

1. **Not a constant futures premium.** GC=F carries a contango vs spot gold,
   which would appear as a fixed offset. It does not explain the variance:
   basis stdev is 0.217% and the range spans 1.84 percentage points. The
   intra-hour-change metric is by construction immune to a steady offset.
2. **Not a session-boundary gap.** Restricting to strictly consecutive hours
   where both series traded continuously, the worst jump is still 1.417% and
   p99 is still 0.318% — both over tolerance. The largest jump occurred at
   2026-07-29 07:00 UTC, mid-session on a Wednesday.
3. **Lower bound, not upper.** This measures XAUT *spot* vs gold. The perp adds
   a further funding-driven basis on top, so true execution-venue tracking
   error is worse than the figures above.

Separately, 231 of 721 hours (32%) had XAUT trading while the gold market was
closed — signal levels quoted against a closed market, executed against a live
one.

### Why it disqualifies GOLD

`MAX_ENTRY_DEVIATION_PERCENT` defaults to 1.5%. A single observed intra-hour
basis move of 1.417% would consume 94% of that entire stale-signal budget
without the underlying asset moving at all. The guard would be measuring basis,
not staleness. A stop placed at a spot-derived level sits at an unpredictable
effective distance, and can be triggered by basis alone.

### Verdict

GOLD/XAU remains **unsupported**. `V1_GOLD_PIN_ACTIVE` stays `True`. This is now
an empirical finding, not a precaution. Revisit only if Kraken lists a genuine
spot XAU/USD perpetual — re-running this study against a different XAUT window
is not sufficient grounds to enable it, because the failure is structural
(separate instrument, separate liquidity, 24/7 vs session-bound).

Raw series: `tests/fixtures/study_a_xaut_gold.json`.

---

## 1C. Study B — leverage state semantics

Status: **API-VERIFIED** (paper engine), 2026-07-29. Both questions answered.

### B(a) — does `set-leverage` affect an already-open position?

**No.** Opened 0.0046 PF_XBTUSD at 10x (liq 59,285.72), then set leverage to
5x. The position was unchanged: still `leverage: 10.0`, liq still 59,285.72.

An add-on order to the *existing* position also retained the position's
original 10x. So a position's leverage is fixed at open time.

**Consequence:** `set-leverage` is safe to call while a position is open — it
cannot silently re-margin live risk. Good news; it means leverage can be
re-asserted freely during recovery and reconciliation.

### B(b) — does leverage persist after the following order fails?

**Yes — confirmed state leak.** Sequence:

1. `set-leverage PF_XBTUSD 3` → succeeded.
2. Next order call failed.
3. `futures paper leverage` → `{"PF_XBTUSD": 3.0}`. The preference persisted.
4. Closed the position so the symbol went flat.
5. Opened a **new** position → it opened at **leverage 3.0**, liquidation
   44,244.68 instead of ~59,300.

A `set-leverage` that succeeded for a trade which never happened silently
governed the *next* signal on that symbol. Margin and liquidation distance
were both wrong relative to intent, with nothing in local state recording it.

### Consequences for the design

- Leverage is exchange state that drifts from local intent, exactly like orders
  and positions. It is not a fire-and-forget parameter.
- The adapter sets leverage as an explicit mutation: read → compare → mutate
  only if different → **verify by reading back**.
- Leverage must be set-and-verified immediately before opening each position,
  never assumed from a previous call.
- Reconciliation (Stage 7) compares on-account leverage per symbol against
  intent and flags drift.
- Recovery (Stage 8) re-asserts leverage for all whitelisted symbols before
  permitting any order — a crash between `set-leverage` and the order is
  exactly case (b). Safe to do because of B(a).

### Caveat

Measured on the paper engine. Kraken documents the same per-symbol preference
model for live, and paper reproduced the documented behaviour, but B(a)/B(b)
should be re-confirmed once live credentials exist. Recorded in §5.

---

## 2. Instrument and product model

Status: **API-VERIFIED** against kraken-cli 0.3.2 on 2026-07-29.

| Canonical | Symbol | contractSize | Size incr. | tickSize | Max lev |
|---|---|---|---|---|---|
| BTC | `PF_XBTUSD` | 1 | 0.0001 | 1 | 100x |
| ETH | `PF_ETHUSD` | 1 | 0.001 | 0.1 | 100x |
| SOL | `PF_SOLUSD` | 1 | 0.01 | 0.01 | 50x |
| XAU | `PF_XAUTUSD` | 1 | 0.001 | 0.1 | 50x |

- `type: flexible_futures`; size is denominated in **base-asset units**.
- Size increment = `10 ** -contractValueTradePrecision`.
- Max leverage = `1 / retailMarginLevels[0].initialMargin`.
- Verified by paper order: `buy PF_XBTUSD 0.0046` produced a position of
  exactly `0.0046 BTC`.
- No spot-gold perpetual exists. `PF_XAGUSD` is silver; `PF_XAUTUSD` is Tether
  Gold (Kraken category: "Stablecoin").

## 3. Order primitives

Status: **API-VERIFIED** (paper engine).

- `--client-order-id` accepted and returned in open-orders. Idempotency anchor.
- `--reduce-only` accepted on stop and take-profit orders.
- `--trigger-signal {mark,index,last}` accepted.
- `edit-order` is an **atomic amend**: amending trigger price and size returned
  the same `order_id`, preserved the client order id, and left the open-order
  count unchanged. No cancel/replace window, so the historical duplicate-stop
  bug is structurally avoidable.
- Positions are **netted per symbol** — one row per symbol with a single side,
  weighted-average entry. No hedge mode, no position ids.

## 4. Known paper-vs-live fidelity gaps

### 4.1 🔴 Paper does NOT enforce the size increment

Measured 2026-07-29. `PF_XBTUSD` has a 0.0001 size increment, yet the paper
engine accepted and filled every one of:

| Requested size | Increment | Paper result |
|---|---|---|
| 0.0001 BTC | 0.0001 | filled |
| 0.00005 BTC | 0.0001 | **filled** (½ increment) |
| 0.00001 BTC | 0.0001 | **filled** (1/10 increment) |
| 0.000001 BTC | 0.0001 | **filled** (1/100 increment) |
| 0.001 SOL | 0.01 | **filled** (1/10 increment) |

Consequences, all of which shape the sizing engine:

1. **Paper cannot validate rounding.** A sizing bug that emits a sub-increment
   quantity passes silently in paper and would be rejected live. Increment
   correctness must therefore be proven by unit tests against the spec, never
   by "it worked in paper".
2. **The exchange is not a safety net.** The sizing engine must enforce the
   increment itself and refuse non-conforming sizes before submission
   (brief 10, 45). We already planned this; this measurement makes it
   mandatory rather than belt-and-braces.
3. **No minimum order size was observable in paper** — because paper enforces
   nothing. Resolved from documentation instead; see §4.1.1.

### 4.1.1 Minimum order size — RESOLVED (doc-verified)

The `instruments` endpoint carries **no** minimum-size field. Verified
exhaustively: all **294** instruments, **82** distinct key paths, zero matches
for `min`, `notional`, `qty`, `quantity`, `lot`, `amount` or `order`. The only
size-related fields are `contractSize`, `tickSize`, `impactMidSize` and
`maxPositionSize` (a maximum, not a minimum).

Kraken publishes the minimums in the linear multi-collateral contract
specifications instead:

| Contract | Published min order size | `10^-contractValueTradePrecision` | Match |
|---|---|---|---|
| PF_XBTUSD | 0.0001 BTC | 0.0001 | ✅ |
| PF_ETHUSD | 0.001 ETH | 0.001 | ✅ |
| PF_SOLUSD | 0.01 SOL | 0.01 | ✅ |

**The minimum order size is exactly one size increment.** The Stage 1 working
assumption was correct, and is now doc-confirmed rather than assumed.

This matters more than it looks: it means the minimum is *derivable from a
field the API does return*, so the sizing engine computes it dynamically from
`contractValueTradePrecision` rather than hardcoding a per-symbol table. No
instrument-specific constants are needed, and a newly listed instrument gets a
correct floor automatically.

**No minimum notional exists** for these contracts — the specifications state
minimums in base-currency units only. Maximum individual order value is
$20,000,000, far above anything a $30-margin bot can produce.

Source:
<https://support.kraken.com/articles/4844359082772-linear-multi-collateral-derivatives-contract-specifications>

Caveat: doc-verified, not API-verified, and the identity min == increment
should be spot-checked live. But it is no longer *blocked* on live credentials,
and a wrong assumption here would now fail closed (we would reject an order
Kraken might have accepted) rather than submitting an invalid one.

Note: the "futures trade in whole contracts only" rule found in Kraken's
support docs applies to **inverse and fixed-maturity** contracts (`PI_`, `FI_`),
not to the `PF_` flexible futures traded here. Empirically confirmed: a
0.0046 BTC `PF_XBTUSD` order filled at exactly that size.

### 4.2 Paper omits `stop_price` on open orders

`futures paper orders` returns `price: null` and no `stop_price` field even for
stop orders, so a stop's trigger price cannot be read back in paper. One leg of
the brief-16 verification (confirm the new stop has the correct trigger price)
is therefore unverifiable in paper; size, existence, type and reduce-only are
all verifiable. Live `open-orders` is expected to return `stopPrice` — **still
unconfirmed, needs live credentials.**

### 4.3 Leverage flag asymmetry

Live `futures order buy|sell` has no `--leverage` flag; paper does. Live
leverage is per-symbol account state via `set-leverage`. The adapter hides
this by always setting the preference and never passing a per-order flag.

### 4.4 No dry-run validation for futures

`--validate` exists on spot orders only. Our DRY_RUN mode therefore stops at
the adapter boundary and never spawns a subprocess — enforced by a constructor
guard on `KrakenCliAdapter`.

---

## 5. Still outstanding before live

| Item | Blocked on | Section |
|---|---|---|
| Withdrawal-permission tests A–D | operator creating test API keys | §1.6 |
| Live `open-orders` returns `stopPrice` | live credentials | §4.2 |
| Re-confirm Study B on live | live credentials | §1C |
| Spot-check min size == increment live | live credentials (non-blocking) | §4.1.1 |

Closed since first written:
- ~~Real minimum order size / min notional~~ — resolved from contract
  specifications; equals one size increment, derivable from the API. §4.1.1
- ~~Whether a key-permission introspection endpoint exists~~ — confirmed
  negative across all 151 commands. §1.4.1

---

## 6. Fill observation (Stage 6)

Status: **API-VERIFIED** (paper engine), 2026-07-29.

### 6.1 `order-status` cannot distinguish filled from cancelled

`futures paper order-status <ID>` searches **open orders only**. Querying a
filled order returns:

```json
{"error":"validation","message":"Order 'FP-00001' not found in open orders"}
```

which is byte-identical to the response for a cancelled order, or for an id
that never existed. So order status alone is unusable as a fill signal.

`fills` carries both `order_id` and `client_order_id`, making it the
authoritative record of what actually executed. Order state is therefore
derived from two reads:

| resting? | fills | derived status |
|---|---|---|
| yes | none | OPEN |
| yes | some | PARTIALLY_FILLED |
| no | == expected | FILLED |
| no | 0 < partial | PARTIALLY_FILLED then removed |
| no | none | **UNKNOWN** — cancelled / rejected / never placed |

The last row is reported as UNKNOWN rather than CANCELLED: those three cases
are indistinguishable from the API, and brief 40 requires reconciling instead
of guessing.

### 6.2 `cancel` takes `--order-id`, not a positional argument

```
kraken futures paper cancel FP-00003     -> error: unexpected argument
kraken futures paper cancel --order-id FP-00003   -> ok
```

Same for live. This escaped the mocked adapter tests, which accepted either
shape, and only failed in the paper integration run — where it left four TP
orders resting after a close and correctly drove the trade to SAFE_ERROR
rather than a false CLOSED.

Argv shape is now pinned by tests for every mutating command. For reference:

| Command | Shape |
|---|---|
| `cancel` | `--order-id <id>` |
| `edit-order` | `--order-id <id>` |
| `set-leverage` | positional `<SYMBOL> <LEVERAGE>` |
| `order buy/sell` | positional `<SYMBOL> <SIZE>` |
| `order-status` | positional `<ORDER_ID>` |

### 6.3 Non-atomic reads in entry observation

Per-order reads and the position read are separate calls, so an entry filling
between them produces PARTIALLY_ENTERED alongside a full-size position. This
errs conservative, protection is sized from the actual position, and repeated
syncs converge — Stage 7's consistent snapshot removes it entirely.

---

## 7. Reconciliation (Stage 7)

Status: **API-VERIFIED** against the paper engine, 2026-07-29.

### 7.1 Verified repairs

Injected genuine out-of-band divergence and confirmed each repair:

| Injected | Detected | Repair |
|---|---|---|
| stop cancelled externally | `UNPROTECTED_POSITION` | stop restored from stored intent |
| stop cancelled externally | `ORDER_MISSING_ON_EXCHANGE` | reported, not guessed as CANCELLED |
| leverage set to 3x out of band | `LEVERAGE_DRIFT` | re-asserted to 10x via the engine |

Second cycle clean in every case, so repairs converge rather than oscillate.

### 7.2 The Stage 6 race, observed and repaired in production conditions

A first reconciliation cycle on an apparently healthy trade flagged
`FILL_NOT_RECORDED`: entry 2 had filled *after* `sync_entry_fills` read it as
OPEN, so the local row disagreed with the exchange. This is the non-atomic-read
race documented in §6.3 — not a false positive, but the safety net catching
what the fast path missed. The consistent snapshot prevents it *within* a
cycle; the loop catches it *across* cycles.

### 7.3 Stop replacement needs a sequence, not a fixed id

Restoring a cancelled stop reused client order id `{trade}-SL-1`, which hit the
UNIQUE constraint on `client_order_id` and left the position **unprotected** —
the precise failure reconciliation exists to repair. Stop client order ids are
now sequenced (`SL-1`, `SL-2`, …). Still deterministic and enumerable, so crash
recovery can walk the sequence and ask the exchange which exist.

### 7.4 WebSocket fast path — untestable without credentials

`futures ws fills` is auth-only with no paper equivalent, so the stream itself
is unverified. Mitigated by design rather than by hope: the stream only *wakes*
the reconciler and never mutates state, so a stream bug degrades latency and
cannot corrupt anything. Line parsing is covered by tests against injected
lines, including malformed input. Added to §5 outstanding.
