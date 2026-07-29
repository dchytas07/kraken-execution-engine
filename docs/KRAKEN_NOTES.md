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
3. **No minimum order size was observable.** Because paper enforces nothing,
   the real floor — whether an increment minimum, a min notional, or both —
   remains unknown. Still outstanding, needs a deliberate live rejection.

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
| Real minimum order size / min notional | live credentials (deliberate rejection) | §4.1 |
| Re-confirm Study B on live | live credentials | §1C |
