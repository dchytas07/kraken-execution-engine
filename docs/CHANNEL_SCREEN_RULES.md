# Channel screen — pass rules

Written 2026-10-09, before any result for a candidate channel was read. These
rules are fixed for this screen. A channel that fails any one of them is out:
no rescue tweaks, no re-runs with changed parameters, no second look with a
different window.

## How a channel is evaluated

- Its signals are replayed with `tools/backtest.py` under the bot's production
  rules and defaults as of this date (sizing, fixed 10x, stop-vs-liquidation
  refusal, stale-entry check, TP ladder, breakeven after TP2, 24h entry
  expiry). Nothing is tuned per channel.
- Baseline costs: taker fee 0.05% on every fill, slippage 0.05% on every
  market-type fill — the tool's defaults.
- **Holdout:** the most recent 6 months of the channel's history, ending at its
  last message at fetch time. Everything earlier is the development period.
  The holdout is evaluated once, after rules 1 and 2 have been decided, and is
  never used to choose or adjust anything.
- Filled trade = a trade whose entry filled and that closed (the report's
  "closed with a position"). Never-filled, cancelled-unfilled and still-open
  trades do not count.

## Rules (all must pass)

1. **Sample size:** at least 300 filled trades in the development period.
2. **Edge survives costs:** with fees and slippage doubled
   (`--fee-mult 2 --slip-mult 2`), the 95% day-clustered bootstrap interval on
   mean per-trade net P&L (`tools/backtest_stats.py`, 10,000 draws, seed
   20261009, development period) lies entirely above zero.
3. **Still works out of sample:** net P&L over the 6-month holdout is positive
   at baseline costs.

A channel is also out if its format cannot be parsed reliably enough to
replay it; that is a screening failure, not a reason to bend the rules.

## Multiple testing

Several channels are screened. Even if none has a real edge, the chance that
at least one passes by luck grows with the number tested. A single pass among
several channels is treated with suspicion, not as a finding: it would need
a further out-of-sample period (new messages after the screen) before any
money or code depends on it. This document says so in advance so the result
cannot talk us out of it.
