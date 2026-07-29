# Stage 10 — Paper soak: pass/fail criteria

Defined **before** the run. A soak without stated criteria is "run it and see",
which cannot fail and therefore proves nothing.

Mode: `TRADING_MODE=paper` against Kraken's live prices. No real funds.

---

## 1. Duration and shape

| Parameter | Value |
|---|---|
| Wall-clock duration | **30 minutes** |
| Reconciliation interval | 60s (the production default, not shortened) |
| Expected cycles | ≥ 28 |
| Process kills | 1, at ~minute 12, via `SIGKILL` (not a graceful stop) |

`SIGKILL` specifically: `SIGTERM` would allow cleanup and would not reproduce a
real crash. The point is to leave the database mid-write and the exchange
holding state the process never recorded.

---

## 2. Invariants checked EVERY cycle

Checked continuously, not only at the end. A violation that self-heals before
the final sweep still counts as a failure — the system spent real time in an
unsafe state.

| # | Invariant |
|---|---|
| I1 | Every open position has **exactly one** protective stop |
| I2 | Every stop's size **equals** its position size |
| I3 | Every TP ladder sums to **≤** its position size |
| I4 | No `client_order_id` appears **more than once** on the exchange |
| I5 | Leverage preference is **10x** on every whitelisted symbol |
| I6 | No trade sits in `SAFE_ERROR` that was not deliberately induced |
| I7 | Every stored message has a **non-PENDING** decision, with a reason if refused |
| I8 | No position exists on a whitelisted symbol without a matching local trade (except the deliberately injected orphan) |

---

## 3. Scenario coverage

Every lifecycle path is exercised by injected synthetic signals.

### Must be REFUSED (and place zero orders)

| # | Scenario | Expected reason |
|---|---|---|
| S1 | Spot / buy-and-hold signal | `SPOT_SIGNAL` |
| S2 | Commentary ("booked 50% earlier") | not executable |
| S3 | Unsupported asset (XRP) | `UNSUPPORTED_ASSET` |
| S4 | GOLD signal | `GOLD_PINNED_OFF` |
| S5 | Incomplete signal (no SL) | `MISSING_FIELDS` |
| S6 | Ambiguous close ("close it") | `NOT_EXECUTABLE_TYPE` (SAFETY_REVIEW) |
| S7 | Stale signal (entry far from mark) | `STALE_ENTRY` |
| S8 | Duplicate message id | already processed |
| S9 | Reformatted duplicate signal | `DUPLICATE_SIGNAL` |
| S10 | Second signal on an occupied symbol | `SYMBOL_ALREADY_ACTIVE` |
| S11 | Signal on a symbol holding an orphan position | `UNRECOGNISED_POSITION` |

### Must EXECUTE correctly

| # | Scenario | Expected |
|---|---|---|
| S12 | Valid LONG, 50x requested | opens at **10x**, two entries, $15/$15 |
| S13 | Valid signal on a second symbol | concurrent trade, independent ladder |
| S14 | TP ladder placement | sums exactly to position, all reduce-only |
| S15 | Partial close (`Book 50%`) | position reduced, stop resized, trade stays open |
| S16 | Full close | confirmed flat → leftovers cancelled → `CLOSED` |

### Injected divergence — reconciler must repair

| # | Injected | Expected repair |
|---|---|---|
| S17 | Cancel a stop out of band | `UNPROTECTED_POSITION` → stop restored from intent |
| S18 | Change leverage out of band to 3x | `LEVERAGE_DRIFT` → re-asserted to 10x |
| S19 | Open a manual position with no local trade | `ORPHAN_POSITION` → reported, **never** auto-closed |

### Crash recovery — the real test

| # | Scenario | Expected |
|---|---|---|
| S20 | `SIGKILL` mid-soak with a live protected position | restart adopts orders/fills, **no duplicates** |
| S21 | Post-restart gate | engine refuses to open before recovery completes |
| S22 | Post-restart protection | position still has exactly one correctly-sized stop |
| S23 | Post-restart leverage | 10x re-asserted |

---

## 4. Pass criteria

The run **passes** only if ALL hold:

1. **Zero invariant violations** across every cycle (I1–I8).
2. **Zero unexplained SAFE_ERROR** — every escalation maps to a deliberately
   injected divergence.
3. **Zero unexplained reconciliation repairs** — every repair maps to an
   injected divergence (S17–S19) or to a known benign catch-up (a fill the
   engine had not yet synced).
4. **Zero duplicate orders** — no `client_order_id` submitted twice, and the
   post-restart order count equals the pre-kill count.
5. **Zero orders from refused signals** — S1–S11 collectively place nothing.
6. **Every refusal carries an enumerated reason** matching the table above.
7. **No unhandled exception** in the bot process.
8. **The full test suite still passes** after the soak.

## 5. Fail criteria

Any one of these fails the run:

- A position observed without a stop, or with more than one, at any cycle.
- A stop whose size does not match its position at any cycle.
- TP total exceeding position size at any cycle.
- Any duplicate `client_order_id` on the exchange.
- A trade marked `CLOSED` while its position is non-flat.
- A refused signal that nonetheless placed an order.
- An orphan position auto-closed by the bot.
- Recovery completing while a position is unprotected.
- Any unhandled exception.

## 6. What this soak does NOT prove

Stated so the result is not over-read:

- **Nothing about live execution.** Paper does not enforce size increments
  (KRAKEN_NOTES 4.1) and omits `stop_price` on open orders (4.2), so those
  remain live-verification items.
- **Nothing about the WebSocket fill stream** — auth-only, no paper equivalent.
- **Nothing about API-key permissions** — needs real credentials.
- **Nothing about real fill dynamics** — slippage, partial fills on thin books,
  and funding are all approximations in paper.

A passing soak means the system's own logic is internally consistent under
sustained operation and a real crash. It does not mean it is ready for live.
