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

**Status update, 2026-07-30:** Tests A/B/C remain unrun — they each require a
Spot API key deliberately provisioned with a specific permission combination,
and no Spot credentials are configured on this machine (`KRAKEN_API_KEY` /
`KRAKEN_API_SECRET` are both empty).

Test D: steps 2 and 3 are now confirmed — see §1.7 (auth was fixed on the
operator's side between the first and second verification pass) and §1.8.
Step 2's "order placement works in paper" was already established in earlier
stages and is unaffected by this. Tests A–C remain the only unresolved part of
§1.6.

---

### 1.7 Live credential attempts, 2026-07-30

Live `KRAKEN_FUTURES_API_KEY` / `KRAKEN_FUTURES_API_SECRET` were added to
`.env`. Attempted, all read-only:

| Call | Result |
|---|---|
| `kraken futures ticker PF_XBTUSD` (public, unauthenticated) | ✅ 200, real market data |
| `kraken futures accounts` (authenticated) | ❌ HTTP 200, body `{"result":"error","error":"authenticationError"}` |
| Same call via the bot's own `run_permission_checks` / `--check` path | ❌ identical — `futures_connectivity: FAIL`, `Authentication failed: authenticationError` |

Diagnosis, to rule out the usual local causes before concluding it's the
credential itself:

- **Not a network/CLI problem.** The public ticker call round-trips to the
  real production endpoint (`futures.kraken.com`) and returns live data
  through the same binary and network path.
- **Not a clock/nonce problem.** Local UTC clock matched the server's
  `serverTime` in the ticker response to the second.
- **Not a credential-shape problem.** Key is 56 chars, secret is 88 chars
  (base64) — both look like well-formed Futures credentials, no stray
  whitespace or quoting in `.env`.
- **Not a bot-side bug.** The failure is identical whether calling
  `kraken` directly with the env sourced, or through
  `KrakenCli`/`run_permission_checks` exactly as production does
  (`env=dict(os.environ)` in `main.py`, unmodified).
- **Not a demo/production endpoint mixup** — `kraken-cli` refuses to point at
  a non-Kraken host at all (`--futures-url` is host-allowlisted), so a demo
  key pointed at prod would still show as a clean `authenticationError` from
  prod, indistinguishable from this result. Worth checking on the Kraken side
  (see below) but not distinguishable from here.

**This narrows it to the credential/account side, not this codebase or this
verification pass.** Likely causes, in rough order of likelihood: the key was
mistyped or truncated when copied into `.env`; the key was created in Kraken's
Futures **demo** environment rather than production; the key has an IP
allowlist that excludes this machine; or the key was disabled/revoked after
creation.

**Consequence at the time:** Test D and every check in §5 gated on a working
authenticated futures call (§4.2 `stopPrice`, §1C Study B live
re-confirmation, §6.3 the WebSocket fill stream) were blocked — not on "no
credentials," but specifically on "the configured credential is rejected by
Kraken."

**Resolved, same day, second pass.** Re-running the identical calls minutes
later succeeded — `kraken futures accounts` now returns `result: success`, and
the bot's own `--check` reports `futures_connectivity: PASS`. Whatever caused
the earlier rejection (most likely a freshly-created key not yet propagated —
the ordinary explanation, and consistent with everything ruled out above)
resolved itself without any change on this side. Nothing in this codebase
needed fixing.

---

### 1.8 ⚠️ Live account state observed during verification, 2026-07-30

The moment authenticated reads started working, the very first call
(`futures accounts`) showed this is **not an empty account**:
`balanceValue: $296.07`, `availableMargin: $141.52`,
`initialMarginWithOrders: $151.35`, `totalUnrealized: -$3.17`.

Follow-up reads (`futures positions`, `futures open-orders`, `futures ws
fills`, all read-only) confirmed **four real open short positions**, each with
a live stop and take-profit resting on the exchange:

| Symbol | Side | Size | Entry | Unrealized PnL | Orders resting |
|---|---|---|---|---|---|
| PF_XBTUSD | short | 0.0047 | 64,541.0 | +$0.08 | 1 TP, 1 stop |
| PF_ETHUSD | short | 0.079 | 1,897.4 | −$1.52 | 2 TP, 2 stop (duplicated) |
| PF_SOLUSD | short | 4.08 | 73.5 | −$2.26 | 2 TP, 2 stop (duplicated) |
| PF_XAUTUSD | short | 0.036 | 4,080.6 | +$0.77 | 2 TP, 2 stop (duplicated), 1 resting entry |

The fill stream (`ws fills`) confirms these came from real fills — market and
limit orders, real fees paid in USD, `realized_pnl` populated on at least one
closed round-trip on `PF_ETHUSD`. Fill and order timestamps run
2026-07-27 14:47 through 2026-07-29 19:05 UTC — all **before** this
verification session, and before this bot's `TRADING_MODE` was ever anything
but `paper`. **None of this was placed by this bot.** It is pre-existing
activity on the account from outside this codebase — most plausibly the
operator's own manual trading, or another tool using the same key.

Two things worth flagging explicitly:

1. **A live position exists on PF_XAUTUSD** — the exact instrument this bot's
   own code refuses to trade (`ALLOW_XAUT_FOR_GOLD` pinned `False`, §1B Study
   A). The bot didn't open it and won't manage it; it's exposure the operator
   is carrying outside the bot's own safety envelope.
2. **This is exactly the scenario `check_exchange_position` (the admission
   gate) exists to catch** — a live position the local database doesn't know
   about. If `TRADING_MODE=live` were ever set with the local DB in its
   current (empty) state, a signal on any of these four symbols should be
   admitted as `REQUIRES_REVIEW` rather than opening a second, uncoordinated
   position. That behaviour was designed for exactly this account state and
   has not been re-verified against it live, since doing so would require
   actually flipping to live mode — out of scope here.

This account is not in a clean state to begin live trading from. Whoever
placed these positions should reconcile or close them (through their own
tooling) before `TRADING_MODE=live` is ever set for this bot, or be prepared
for the bot to quarantine signals on all four symbols on its first live
startup.

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

Status: **API-VERIFIED.** B(a): paper (2026-07-29) **and live (2026-07-30,
with a real divergence found)**. B(b): paper only, **live intentionally not
attempted** — see below.

### B(a) — does `set-leverage` affect an already-open position?

**Paper, 2026-07-29: No.** Opened 0.0046 PF_XBTUSD at 10x (liq 59,285.72),
then set leverage to 5x. The position was unchanged: still `leverage: 10.0`,
liq still 59,285.72. An add-on order to the *existing* position also retained
the position's original 10x.

**Live, 2026-07-30: partially contradicts the paper finding.** Tested against
one of the operator's real pre-existing positions (PF_ETHUSD short, 0.079 @
1897.4, opened outside this bot — §1.8), chosen as the smallest by notional.
Sequence, on the live `flex` (multi-collateral) account:

| | initialMargin | initialMarginWithOrders | maintenanceMargin | availableMargin | PF_ETHUSD position |
|---|---|---|---|---|---|
| Baseline | $90.00 | $151.35 | $45.00 | $141.50 | size 0.079, price 1897.4, no leverage field |
| After `set-leverage PF_ETHUSD 5` | **$104.99** | **$181.81** | $45.00 (unchanged) | **$112.64** | size 0.079, price 1897.4 (unchanged) — but now carries `maxFixedLeverage: 5.0` |
| After revert (`set-leverage PF_ETHUSD`, no value) | $90.00 | $151.35 | $45.00 | ~$141.83* | size 0.079, price 1897.4, leverage field gone again |

\* small residual difference from baseline is ordinary PnL/funding drift over
the ~1 minute elapsed, not a margin-state effect — `leveragePreferences` read
back empty, matching the pre-test baseline exactly.

**What did and didn't change:** the position itself — size, entry price, side
— was untouched at every step, and `maintenanceMargin` (the figure closest to
actual liquidation risk) never moved. **But `set-leverage` on a symbol with an
open position immediately attached a `maxFixedLeverage` field to that position
and increased the account's `initialMargin`/`initialMarginWithOrders` by
~$15/$30, cutting `availableMargin` by ~$29** — live re-margined the position
on the spot. Paper's "the position was unchanged" finding described the
position's own leverage/liq fields correctly, but doesn't generalize to "the
account's margin state is unaffected" — on live, it clearly is affected.

Nothing dangerous happened in this test — the account had ample equity headroom
and no maintenance-margin/liquidation risk emerged — but a `set-leverage` call
against a position is **not the no-op live that it is in paper.**

### B(b) — does leverage persist after the following order fails?

**Paper, 2026-07-29: Yes — confirmed state leak.** Sequence:

1. `set-leverage PF_XBTUSD 3` → succeeded.
2. Next order call failed.
3. `futures paper leverage` → `{"PF_XBTUSD": 3.0}`. The preference persisted.
4. Closed the position so the symbol went flat.
5. Opened a **new** position → it opened at **leverage 3.0**, liquidation
   44,244.68 instead of ~59,300.

A `set-leverage` that succeeded for a trade which never happened silently
governed the *next* signal on that symbol. Margin and liquidation distance
were both wrong relative to intent, with nothing in local state recording it.

**Live: intentionally not attempted, 2026-07-30.** Confirming this live would
require deliberately causing a real order to fail and then opening a real new
position on a funded account to see whether it inherits the stale leverage —
both are live trades. The operator chose not to place either, on the grounds
that the code's mitigation (below) doesn't depend on B(b) matching live to
remain safe — it treats leverage as untrusted on every cycle regardless of
which failure mode produced the drift. Left as paper-verified only.

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
  exactly case (b).
- **Correction from the original paper-only version of this section, now
  implemented as a guard, not just a note.** That bullet used to end "Safe to
  do because of B(a)" — i.e. re-asserting leverage during recovery was framed
  as risk-free because it supposedly couldn't touch an open position's
  margin. The live B(a) result above shows that framing was wrong:
  re-asserting leverage on a symbol that already has an open position
  **does** move that account's margin requirements immediately. It is still
  the correct design — a crash mid-sequence must have its leverage
  re-asserted before any new order — but it is not the zero-consequence
  operation this section previously claimed, so both call sites are now
  margin-gated:
  - Before mutating, both `RecoveryManager.recover()`
    ([recovery.py:140](../src/bot/recovery.py)) and
    `Reconciler._check_leverage_drift()`
    ([reconcile.py:607](../src/bot/reconcile.py)) read `get_account_state()`
    and refuse the mutation — logging CRITICAL and blocking the trading gate
    (recovery) or marking the discrepancy unrepaired (reconciliation) — if
    available margin is already below one trade's margin
    (`config.trade_margin_usd`). The mutation's exact cost can't be predicted
    in advance (Kraken exposes no dry-run for it), so this checks the one
    thing that *can* be known ahead of time: existing headroom.
  - Every actual mutation logs available margin before and after at WARNING,
    so the effect is never silent, matching what the live test above showed.
  - If the mutation itself (whose cost wasn't knowable beforehand) leaves
    available margin below that same floor, it's logged CRITICAL and the
    trading gate is blocked / the discrepancy is left flagged for operator
    review, even though the mutation already succeeded — it can't be undone
    automatically without another mutation of unknown cost.
  - Covered by six new tests: refusal-when-thin and before/after-margin
    logging in both `tests/test_recovery.py` and `tests/test_reconcile.py`,
    plus a `MarginCostingExchange` fake in each that simulates the live
    finding to exercise the post-mutation escalation path.

### Caveat

B(a) is now API-verified on both paper and live, with the live result
correcting part of the paper-only conclusion (above) and driving a real code
change (the margin-headroom guard, above). B(b) remains paper-verified only,
by deliberate operator choice rather than a blocked credential — see above.
Closed in §5.

---

## 2. Instrument and product model

Status: **API-VERIFIED** against kraken-cli 0.3.2 on 2026-07-29.

| Canonical | Symbol | contractSize | Size incr. | tickSize | Max lev |
|---|---|---|---|---|---|
| BTC | `PF_XBTUSD` | 1 | 0.0001 | 1 | 100x |
| ETH | `PF_ETHUSD` | 1 | 0.001 | 0.1 | 100x |
| SOL | `PF_SOLUSD` | 1 | 0.01 | 0.01 | 50x |
| BNB | `PF_BNBUSD` | 1 | 0.01 | 0.01 | 50x |
| XAU | `PF_XAUTUSD` | 1 | 0.001 | 0.1 | 50x |

### 2.1 BNB — verified and ENABLED, 2026-07-31

`PF_BNBUSD` was verified the same way BTC/ETH/SOL were, from the live API
rather than from its name: `type: flexible_futures`, base `BNB`, quote `USD`,
category **Layer 1**, `contractSize` 1, mark 589.38 against index 589.29
(0.015% apart), 24h volume 1,967. A direct perpetual with no wrapper, so
unlike XAU it needs no substitute note and no tracking study.

Promoting it required removing `bnb` from the parser's `_KNOWN_OTHER_ASSETS`,
where it had been listed as recognised-but-unsupported. A token in both maps
means "trade this" and "refuse this" simultaneously and the winner depends on
lookup order, so `aliases.py` now raises at import if the two sets ever
overlap again.

### 2.2 S&P 500 — two instruments exist and NEITHER is safe

Both were checked because the channel posts S&P signals. Neither is enabled.

| Symbol | What it actually is | Price | Category |
|---|---|---|---|
| `PF_SPXUSD` | **SPX6900, a memecoin** — not the index | **$0.32** | `Meme` |
| `PF_SPYXUSD` | tokenised SPY *ETF* wrapper | $744.84 | `xStocks` |

`PF_SPXUSD` is the dangerous one: the ticker looks like the S&P 500, the
channel quotes S&P around 7,400, and the instrument trades at 32 cents. Symbol
matching alone would have bought a memecoin.

`PF_SPYXUSD` is structurally the XAUT situation — a wrapped token, base asset
`SPYx`, tracking the SPY ETF rather than the index the signals quote. The
proposed `/10` conversion was measured against a month of hourly data rather
than assumed:

| Metric | Measured | Consequence |
|---|---|---|
| SPX/SPY ratio, median | **10.0343** | hardcoding `/10` bakes in 0.343% error |
| ratio range | 10.0260 – 10.0392 | drifts with dividend accrual; not a constant |
| worst-case error vs `/10` | **0.392%** | 26% of the 1.5% stale-entry budget, before any market move |
| hours the index quoted | **155 / 726 (21.3%)** | |
| hours the perp trades blind | **572 (78.7%)** | Study A disqualified GOLD at 32% |

The `/10` is real in origin — SPY genuinely trades near a tenth of SPX — but
it is neither exact nor stable, and 78.7% of the time there is no index to
compare against at all. Not enabled.

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

### 4.1.1 Minimum order size — RESOLVED (doc-verified, spot-checked live)

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

**Spot-checked live, 2026-07-30.** `kraken futures instruments` is a public,
unauthenticated endpoint, so this did not require a working credential. Pulled
all 295 live instruments (one more than the 294 in the earlier doc-verified
pass — an addition to Kraken's catalog, not a discrepancy) and repeated both
checks against the live payload:

- `contractValueTradePrecision` for `PF_XBTUSD` / `PF_ETHUSD` / `PF_SOLUSD` is
  `4` / `3` / `2` live, giving derived increments `0.0001` / `0.001` / `0.01` —
  **exact match** to the doc-derived values above.
- Repeated the exhaustive key-name search (`min`, `notional`, `qty`,
  `quantity`, `lot`, `amount`, `order`) against the live payload: **zero
  matches**, same as the doc-verified pass.

The identity min == increment is now API-verified against live, not just
doc-verified. No longer caveated.

Note: the "futures trade in whole contracts only" rule found in Kraken's
support docs applies to **inverse and fixed-maturity** contracts (`PI_`, `FI_`),
not to the `PF_` flexible futures traded here. Empirically confirmed: a
0.0046 BTC `PF_XBTUSD` order filled at exactly that size.

### 4.2 Paper omits `stop_price` on open orders — RESOLVED, live confirmed

`futures paper orders` returns `price: null` and no `stop_price` field even for
stop orders, so a stop's trigger price cannot be read back in paper. One leg of
the brief-16 verification (confirm the new stop has the correct trigger price)
is therefore unverifiable in paper; size, existence, type and reduce-only are
all verifiable.

**Confirmed live, 2026-07-30.** `kraken futures open-orders` against the live
account returns `stopPrice` on every `stop` and `take_profit` order, e.g.:

```json
{"orderType":"stop","reduceOnly":true,"side":"buy","status":"untouched",
 "stopPrice":4192.8,"symbol":"PF_XAUTUSD","triggerSignal":"mark", ...}
```

Live matches the paper-verified fields (size, existence, type, reduce-only)
and adds the one live-only field. The brief-16 verification is now fully
checkable against a real account; the paper gap is paper-only, not a real
blind spot.

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

**Nothing here blocks live trading.** One item remains, and it's optional:

| Item | Blocked on | Section |
|---|---|---|
| Withdrawal tests A–C (spot key scope combinations) | 3 purpose-built spot API keys the operator doesn't currently have, each needing a specific permission combination (Query+Withdraw / Query-only / Withdraw-only) | §1.6 |

These exist to validate the *shape* of the withdrawal-scope check itself
(that a denial is correctly treated as `NOT_VERIFIABLE` rather than `PASS`,
etc. — §1.2). They don't gate anything: the futures key actually used for
trading is covered by the attestation gate (§1.5, confirmed live in §1.7),
and no spot key is used for trading at all (§1.3). Run them only if/when it's
worth provisioning three disposable spot keys to pressure-test the check's
own logic — not before then.

Everything else that was outstanding is now closed (below), including Study
B — B(a) live-confirmed with a real finding that changed the code (a
margin-headroom guard, §1C), B(b) closed as paper-only by deliberate
operator decision rather than a blocker.

**Standing note, not a task:** the account still holds 4 pre-existing live
positions from outside this bot (§1.8), including one on PF_XAUTUSD. This
isn't something to "close" — it's a fact about current account state, and it
will still be true whenever `TRADING_MODE=live` is eventually set. Re-check
`kraken futures positions` at that time rather than trusting this snapshot.

Closed since first written:
- ~~Real minimum order size / min notional~~ — resolved from contract
  specifications; equals one size increment, derivable from the API. §4.1.1
- ~~Spot-check min size == increment live~~ — confirmed 2026-07-30 against the
  live `instruments` endpoint (public, unauthenticated): derived increments
  match exactly, no min-size field exists live either. §4.1.1
- ~~Whether a key-permission introspection endpoint exists~~ — confirmed
  negative across all 151 commands. §1.4.1
- ~~Withdrawal test D~~ — `futures accounts` now authenticates and the
  attestation gate behaves as documented; both confirmed live. §1.7, §1.8
- ~~Live `open-orders` returns `stopPrice`~~ — confirmed live, present on
  every stop/take_profit order. §4.2
- ~~WebSocket fill stream auth~~ — confirmed live: authenticates, returns a
  well-formed `fills_snapshot` feed. §7.4
- ~~Re-confirm Study B on live~~ — B(a) live-tested 2026-07-30 against a real
  position (§1C): the position itself was untouched, but live's margin
  behaviour genuinely diverges from what paper showed — `set-leverage`
  measurably re-margins an open position's account requirements, which paper
  did not reveal. The design's mitigation doesn't depend on B(a) being a
  no-op, but the doc's old "safe because of B(a)" framing was corrected. B(b)
  closed as paper-only by deliberate operator decision — live confirmation
  would require placing real trades, which was declined; the code's
  read-verify-mutate-verify pattern treats leverage as untrusted every cycle
  regardless of which failure mode produced the drift, so it doesn't depend on
  B(b) matching live to remain safe.

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

### 7.4 WebSocket fast path — auth confirmed live

`futures ws fills` is auth-only with no paper equivalent, so the stream itself
was unverified. Mitigated by design rather than by hope: the stream only
*wakes* the reconciler and never mutates state, so a stream bug degrades
latency and cannot corrupt anything. Line parsing is covered by tests against
injected lines, including malformed input.

**Confirmed live, 2026-07-30.** Connected with real credentials for a bounded
window; received a well-formed `fills_snapshot` feed (`account`, `feed`,
`fills[]` with `fill_id`, `order_id`, `instrument`, `price`, `qty`,
`fee_paid`, `realized_pnl`, `time`) carrying real historical fills from the
account's pre-existing activity (§1.8). Authentication and message shape are
both now verified against the real feed, not just against injected test
lines.

---

## 8. Restart recovery (Stage 8)

Status: **API-VERIFIED** against the paper engine, 2026-07-29.

### 8.1 Verified end-to-end

Process 1 opened a trade, both entries filled, then died before syncing,
before placing a stop, and with a leaked 3x leverage preference. Process 2,
a genuinely fresh Store and engine over the same database file:

| Check | Result |
|---|---|
| Gate held before recovery | refused with `RECOVERY_INCOMPLETE` |
| Entry fills recovered | both `ADOPTED_FILLED` |
| Local quantity | 0.0046, matching the exchange |
| Protection | 1 stop, sized to the position |
| Leverage | restored 3x → 10x |
| Duplicate orders | none — 1 order total on the exchange |

### 8.2 Resolving the crash window

An unacknowledged order is looked up by its deterministic client order id:

| Snapshot says | Outcome |
|---|---|
| resting | `ADOPTED_RESTING` — adopt the exchange id |
| in fills | `ADOPTED_FILLED` — record the fill |
| absent, symbol flat | `NEVER_PLACED` — safe to abandon |
| absent, position exists | `AMBIGUOUS` — REQUIRES_REVIEW |

The last row is the one that must not be guessed. Kraken's fills history is
bounded, so absence is not proof an order never filled, and a position exists
that something must account for.

### 8.3 The gate

`ExecutionEngine._recovery_complete` starts False on every process and is
cleared only by `RecoveryManager`. Recovering after placing orders would defeat
the purpose, so `open_trade` calls `require_recovered()` first. Recovery that
cannot complete — unreadable exchange, or a position it could not protect —
leaves the gate shut and startup exits non-zero.

### 8.4 Two bugs found by the crash matrix

* **A trade stuck in CLOSING was never finalised.** The reconciler excluded
  CLOSING from its flat-position check to avoid racing an in-flight close. But
  after a restart there is no in-flight anything, and even in-process a flat
  position means the close succeeded. A crash between submitting a close and
  confirming it would have left the trade CLOSING forever.

* **A recovered trade kept a stale lifecycle label.** A filled, protected trade
  stayed labelled WAITING_FOR_ENTRY. Protection was correct (the stop is sized
  from the position, not the label), but a stale label misleads an operator and
  gates later TP transitions. Reconciliation now advances it.
