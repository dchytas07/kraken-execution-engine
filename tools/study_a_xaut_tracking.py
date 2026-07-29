"""Study A — XAUT vs spot-gold tracking error (docs/PLAN.md 4.4.1, 7a).

Measures whether PF_XAUTUSD is a safe execution venue for GOLD signals whose
entry/TP/SL levels are quoted against spot gold.

Series 1: Kraken XAUTUSD spot hourly (the perp's underlying).
Series 2: COMEX front-month gold futures (GC=F) hourly, via Yahoo.

Caveats recorded with the result, not buried:
  * GC=F is a futures contract, so it carries its own small basis to spot gold.
  * GC=F trades COMEX hours; XAUT trades 24/7. Non-overlapping hours are
    excluded from the paired stats but counted separately, because "gold market
    closed while XAUT keeps moving" is itself a divergence source.
  * We compare XAUT *spot* to gold. The perp adds a further funding-driven
    basis on top, so the figure here is a LOWER BOUND on total tracking error.
"""

import json
import statistics
import subprocess
import urllib.request
from datetime import UTC, datetime

TOL_P99 = 0.5      # percent
TOL_INTRA_HOUR = 0.25  # percent


def kraken_xaut_hourly():
    out = subprocess.run(
        ["kraken", "ohlc", "XAUTUSD", "--interval", "60", "-o", "json"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    data = json.loads(out)
    key = next(k for k in data if k != "last")
    return {int(c[0]): float(c[4]) for c in data[key]}  # ts -> close


def yahoo_gold_hourly():
    req = urllib.request.Request(
        "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=1mo",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    close = res["indicators"]["quote"][0]["close"]
    # Snap to the top of the hour so the two series align.
    return {(t // 3600) * 3600: c for t, c in zip(ts, close) if c is not None}


def pct(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(int(round(p / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def main():
    xaut = kraken_xaut_hourly()
    gold = yahoo_gold_hourly()

    common = sorted(set(xaut) & set(gold))
    print(f"XAUT hourly points : {len(xaut)}")
    print(f"GC=F hourly points : {len(gold)}")
    print(f"overlapping hours  : {len(common)}")
    if common:
        print(f"window             : {datetime.fromtimestamp(common[0], UTC)} "
              f"-> {datetime.fromtimestamp(common[-1], UTC)}")
    print(f"XAUT-only hours    : {len(set(xaut) - set(gold))} "
          f"(gold market closed, XAUT still trading)")

    if len(common) < 100:
        print("\nINSUFFICIENT OVERLAP — cannot conclude.")
        return

    basis = [(xaut[t] - gold[t]) / gold[t] * 100 for t in common]
    absb = [abs(b) for b in basis]

    # Largest change in basis across one hour — the metric that threatens a stop.
    deltas = [
        abs(basis[i] - basis[i - 1])
        for i in range(1, len(common))
        if common[i] - common[i - 1] == 3600
    ]

    print("\n--- basis (XAUT vs GC=F), percent ---")
    print(f"  mean signed   : {statistics.mean(basis):+.4f}%")
    print(f"  median signed : {statistics.median(basis):+.4f}%")
    print(f"  median |basis|: {statistics.median(absb):.4f}%")
    print(f"  p95  |basis|  : {pct(absb, 95):.4f}%")
    print(f"  p99  |basis|  : {pct(absb, 99):.4f}%")
    print(f"  max  |basis|  : {max(absb):.4f}%")
    print(f"\n--- intra-hour basis change ({len(deltas)} consecutive pairs) ---")
    print(f"  median : {statistics.median(deltas):.4f}%")
    print(f"  p95    : {pct(deltas, 95):.4f}%")
    print(f"  p99    : {pct(deltas, 99):.4f}%")
    print(f"  max    : {max(deltas):.4f}%")

    p99 = pct(absb, 99)
    mx = max(deltas)
    print("\n--- verdict ---")
    print(f"  p99 |basis|        {p99:.4f}%  vs tolerance {TOL_P99}%   "
          f"{'PASS' if p99 < TOL_P99 else 'FAIL'}")
    print(f"  max intra-hour     {mx:.4f}%  vs tolerance {TOL_INTRA_HOUR}%  "
          f"{'PASS' if mx < TOL_INTRA_HOUR else 'FAIL'}")
    print(f"\n  STUDY A: {'PASS' if (p99 < TOL_P99 and mx < TOL_INTRA_HOUR) else 'FAIL'}")

    with open("study_a_data.json", "w") as f:
        json.dump(
            {"xaut": xaut, "gold": gold, "common": common, "basis": basis}, f
        )


if __name__ == "__main__":
    main()
