"""Uncertainty on a backtest run: day-clustered bootstrap over per-trade net P&L.

READ-ONLY. Reads the .jsonl that tools/backtest.py writes next to its report.

    .venv/bin/python tools/backtest_stats.py data/backtest_cache/report.jsonl
    .venv/bin/python tools/backtest_stats.py RUN.jsonl --exclude-quarters 2025-Q4 2026-Q1

Trades are clustered by the UTC calendar day their signal was posted: signals
on the same day share one market move, so resampling single trades would
treat correlated outcomes as independent and understate the spread. Each draw
resamples the days with replacement (as many days as there are) and takes
every closed trade of each drawn day.

Closed = filled and finished, the same set the report's NET counts: never-
filled, cancelled-unfilled and still-open trades are excluded.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from datetime import datetime

EXCLUDED_OUTCOMES = (None, "NEVER_FILLED", "CANCELLED_BY_CHANNEL")


def quarter(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def load_closed(path: str) -> list[dict]:
    rows = [json.loads(line) for line in open(path)]
    return [r for r in rows
            if r.get("verdict") == "OPENED" and r.get("outcome") not in EXCLUDED_OUTCOMES]


def max_drawdown(trades: list[dict]) -> tuple[float, float]:
    """As the report: realised equity in close-time order."""
    eq = peak = dd = 0.0
    for r in sorted(trades, key=lambda r: r["closed_at"]):
        eq += r["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd, peak


def bootstrap(trades: list[dict], draws: int, seed: int) -> dict:
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in trades:
        by_day[r["date"][:10]].append(r["net"])
    days = list(by_day.values())
    rng = random.Random(seed)
    totals, means = [], []
    for _ in range(draws):
        total = count = 0
        for day in rng.choices(days, k=len(days)):
            total += sum(day)
            count += len(day)
        totals.append(total)
        means.append(total / count)
    totals.sort()
    means.sort()

    def pct(xs, q):
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    return {
        "days": len(days),
        "total_ci": (pct(totals, 0.025), pct(totals, 0.975)),
        "mean_ci": (pct(means, 0.025), pct(means, 0.975)),
        "p_total_le_0": sum(1 for t in totals if t <= 0) / draws,
    }


def money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def summary(label: str, trades: list[dict], draws: int, seed: int) -> str:
    nets = [r["net"] for r in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    dd, peak = max_drawdown(trades)
    b = bootstrap(trades, draws, seed)
    lines = [
        label,
        f"  closed trades                  {len(nets):12d}   on {b['days']} signal days",
        f"  NET                            {money(sum(nets)):>12s}",
        f"  per closed trade               {money(statistics.fmean(nets)):>12s}",
        f"  win rate                       {len(wins) / len(nets) * 100:11.1f}%",
        f"  average win / average loss     {money(statistics.fmean(wins))} / "
        f"{money(statistics.fmean(losses))}",
        f"  breakeven-stop exits           {sum(r['outcome'] == 'BREAKEVEN_STOP' for r in trades):12d}",
        f"  fees / funding                 {money(-sum(r['fees'] for r in trades))} / "
        f"{money(-sum(r['funding'] for r in trades))}",
        f"  max drawdown (realised)        {money(-dd):>12s}   (equity peak before it {money(peak)})",
        f"  95% CI, mean per trade         {money(b['mean_ci'][0])} .. {money(b['mean_ci'][1])}",
        f"  95% CI, total P&L              {money(b['total_ci'][0])} .. {money(b['total_ci'][1])}",
        f"  share of draws with total <= 0 {b['p_total_le_0'] * 100:11.1f}%",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("jsonl")
    ap.add_argument("--label", default="run")
    ap.add_argument("--exclude-quarters", nargs="*", default=[],
                    help="drop trades whose SIGNAL falls in these quarters, e.g. 2025-Q4")
    ap.add_argument("--draws", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=20261009)
    args = ap.parse_args(argv)

    trades = load_closed(args.jsonl)
    if args.exclude_quarters:
        trades = [r for r in trades if quarter(r["date"]) not in args.exclude_quarters]
    print(summary(args.label, trades, args.draws, args.seed))
    print(f"  (day-clustered bootstrap, {args.draws:,} draws, seed {args.seed}; "
          "percentile intervals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
