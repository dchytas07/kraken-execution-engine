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

- `futures paper orders` omits `stop_price` (returns `price: null` on stops),
  so stop trigger price cannot be verified in paper. Live `open-orders` is
  expected to include it — **to be confirmed (Stage 4)**.
- Live `futures order buy|sell` has no `--leverage` flag; paper does. Live
  leverage is per-symbol account state via `set-leverage`.
- No `--validate` dry-run flag on futures orders (spot only).
