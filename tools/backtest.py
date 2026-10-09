"""Backtest the channel's signals against real Kraken candles, under the bot's rules.

READ-ONLY RESEARCH. Touches no database, no service, no account. Reads
corpus/channel_history.jsonl, fetches public market data, and prints a report.

    .venv/bin/python tools/backtest.py            # full run (fetches + caches candles)
    .venv/bin/python tools/backtest.py --offline  # cached candles only

What it reuses from the bot (so the rules cannot drift from production):
    parse_message, classify_message      which messages are signals / instructions
    fingerprint_for                      duplicate-signal detection
    split_entry_allocations, calculate_size, split_take_profits
    max_leverage_for_stop, stop_distance, is_entry_stale, round_price_to_tick

What it re-implements (there is no pure function to call; each mirrors the
engine and the file/line it mirrors is named where it happens):
    admission order, the trade lifecycle (entries -> stop -> TP ladder ->
    breakeven -> close), 24h entry expiry, channel stop updates / closes /
    cancels and how they are linked to a trade.

Data:
    1. Kraken Futures 1-minute TRADE candles for the PF_ symbols, from the public
       charts API (kraken-cli has no futures OHLC command). Available from
       2022-03-23 (DOGE 2022-06-20, BNB 2023-11-22).
    2. Fallback: Kraken spot OHLC via `kraken ohlc`. That endpoint returns only
       the most recent 720 candles at any interval, so before the PF_ history
       the finest spot candle that reaches back is WEEKLY. A weekly candle spans
       the stop and every TP of a typical signal, so it cannot say which came
       first; such signals are reported as NOT COVERED rather than guessed.
    3. Funding: `kraken futures historical-funding-rates` (hourly, about the
       last 12 months). Older hours use that symbol's mean hourly rate.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.exchange.specs import parse_instrument  # noqa: E402
from bot.models import CanonicalAsset, Direction, MessageAction, MessageType  # noqa: E402
from bot.parser.message_class import classify_message  # noqa: E402
from bot.parser.parse import fingerprint_for, parse_message  # noqa: E402
from bot.sizing import (  # noqa: E402
    SizingError,
    calculate_size,
    is_entry_stale,
    max_leverage_for_stop,
    round_price_to_tick,
    split_entry_allocations,
    split_take_profits,
)

D = Decimal

# --------------------------------------------------------------------------
# Rules (production defaults; see src/bot/config.py) and stated assumptions
# --------------------------------------------------------------------------

TRADE_MARGIN = D(30)                 # TRADE_MARGIN_USD: $15 per entry x 2
LEVERAGE = D(10)                     # FIXED_LEVERAGE
STOP_LIQ_BUFFER_PCT = D("1.0")       # => stop more than 7% from entry refused
MAX_ENTRY_DEVIATION_PCT = D("1.5")   # stale-entry guard at placement
MAX_SIZING_DEVIATION_PCT = D("5.0")
MAX_TRADES_PER_SYMBOL = 2
ENTRY_EXPIRY_S = 24 * 3600
MAINTENANCE = 0.02                   # liquidation estimate, as sizing.py

TAKER_FEE = 0.0005                   # Kraken Futures base tier, every fill
SLIPPAGE = 0.0005                    # adverse, on every market-type fill

# --costs favourable: the maker fee only where the real engine's order rests on
# the book (engine.py open_trade: plain "limit" entries, no post-only, so an
# entry already through the market at placement crosses as a taker); every
# other order is a trigger or market order (stop, take-profit with no limit
# price, market close) and pays taker. Slippage on stop exits only.
COST_MODEL = "default"
MAKER_FEE = 0.0002
FAVOURABLE_STOP_SLIPPAGE = 0.0002
FILLS: Counter = Counter()           # (fill kind, fee kind) -> count

ASSUMPTIONS = [
    "Sizing, leverage, the stop-vs-liquidation refusal, the stale-entry check and "
    "the TP split use the bot's own functions with production defaults: $30 margin "
    "per trade split $15/$15 over two limit entries, fixed 10x, 1.0pt liquidation "
    "buffer (stops more than 7% from the farther entry refused), 1.5% stale-entry "
    "limit, 2 same-direction trades per symbol, 24h entry expiry.",
    "Breakeven follows the ENGINE: the stop moves to the average fill price after a "
    "CONFIRMED TP2 fill (manager.py step 5, brief 15), not after TP1. A TP1 variant "
    "is reported separately for comparison only.",
    "Taker fee 0.05% on every fill, entries and limit TPs included (conservative: "
    "resting limits would really pay the 0.02% maker fee).",
    "Slippage 0.05% against us on every market-type fill: stop exits, TP triggers "
    "(Kraken take-profit orders execute at market), channel closes, and entries "
    "that were already marketable when placed. Resting limit entries fill at their "
    "limit price.",
    "Funding: actual hourly Kraken rates where published (~last 12 months), else "
    "the symbol's mean hourly rate over that period. Longs pay a positive rate.",
    "Candles are 1-minute TRADE prices; the bot's stops and TPs trigger on MARK "
    "price, which is not available historically at this resolution.",
    "Inside one candle the worse outcome is assumed: a stop touched in the same "
    "minute as a TP or an entry fills first; a TP is never filled in the minute "
    "its position was opened.",
    "A stop gapped through fills at the candle open; if that is beyond the "
    "estimated liquidation price the loss is capped there (liquidation).",
    "Channel instructions are applied as the bot would: strict-grammar closes "
    "close at market, cancels end trades with no fills, stop updates tighten "
    "only. Everything the classifier routes to a human is NOT acted on.",
    "The corpus holds each message's LATEST text; a signal edited after posting "
    "is simulated as edited. The bot would have seen the original.",
    "MAX_TOTAL_OPEN_MARGIN is not modelled (its live value is in .env, which this "
    "tool does not read); at most 2 trades x 6 symbols x $30 = $360 can be open.",
    "Instrument tick and size increments are today's; historical ones may differ.",
    "Signals are processed at their Telegram post minute (age 0): the "
    "MAX_SIGNAL_AGE_MINUTES gate never fires in a replay.",
]

TRADEABLE = {
    CanonicalAsset.BTC: "PF_XBTUSD",
    CanonicalAsset.ETH: "PF_ETHUSD",
    CanonicalAsset.SOL: "PF_SOLUSD",
    CanonicalAsset.BNB: "PF_BNBUSD",
    CanonicalAsset.XRP: "PF_XRPUSD",
    CanonicalAsset.DOGE: "PF_DOGEUSD",
}
SPOT_PAIR = {
    "PF_XBTUSD": "XBTUSD", "PF_ETHUSD": "ETHUSD", "PF_SOLUSD": "SOLUSD",
    "PF_BNBUSD": "BNBUSD", "PF_XRPUSD": "XRPUSD", "PF_DOGEUSD": "XDGUSD",
}

CACHE = ROOT / "data" / "backtest_cache"       # data/ is gitignored
CHUNK_S = 2000 * 60                              # the charts API's page size


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

class Candles:
    """1-minute PF_ trade candles, fetched in aligned 2000-minute chunks."""

    URL = "https://futures.kraken.com/api/charts/v1/trade/{sym}/1m?from={a}&to={b}"

    def __init__(self, symbol: str, offline: bool) -> None:
        self.symbol = symbol
        self.offline = offline
        self._chunks: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        self.requests = 0

    def _path(self, k: int) -> Path:
        return CACHE / self.symbol / f"{k}.json.gz"

    def chunk(self, k: int) -> dict[int, tuple[float, float, float, float]]:
        if k in self._chunks:
            return self._chunks[k]
        path = self._path(k)
        data = None
        if path.exists():
            data = json.loads(gzip.decompress(path.read_bytes()))
        elif not self.offline:
            a, b = k * CHUNK_S, (k + 1) * CHUNK_S - 1
            url = self.URL.format(sym=self.symbol, a=a, b=b)
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(url, timeout=30) as resp:
                        raw = json.loads(resp.read())
                    break
                except Exception:  # noqa: BLE001 — retry, then give up
                    if attempt == 3:
                        raise
                    time.sleep(2 ** attempt)
            self.requests += 1
            data = [[c["time"] // 1000, float(c["open"]), float(c["high"]),
                     float(c["low"]), float(c["close"])] for c in raw.get("candles", [])]
            # A chunk still being written (reaching into the last hour) is not cached.
            if b < time.time() - 3600:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(json.dumps(data).encode()))
        out = {row[0]: tuple(row[1:]) for row in (data or [])}
        self._chunks[k] = out
        if len(self._chunks) > 400:                 # bound memory
            self._chunks.pop(next(iter(self._chunks)))
        return out

    def get(self, minute: int):
        return self.chunk(minute // CHUNK_S).get(minute)

    def covers(self, t: float, within_s: int = 600) -> bool:
        m = int(t // 60) * 60
        return any(self.get(m + i * 60) is not None for i in range(within_s // 60))


class SpotProbe:
    """Finest Kraken spot OHLC interval (via kraken-cli) whose history reaches t."""

    INTERVALS = (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)

    def __init__(self, offline: bool) -> None:
        self.offline = offline
        self._earliest: dict[tuple[str, int], float | None] = {}

    def earliest(self, pair: str, interval: int) -> float | None:
        key = (pair, interval)
        if key not in self._earliest:
            self._earliest[key] = None
            if not self.offline:
                try:
                    out = subprocess.run(
                        ["kraken", "ohlc", pair, "--interval", str(interval),
                         "--since", "1", "-o", "json"],
                        capture_output=True, text=True, timeout=60, check=False)
                    candles = json.loads(out.stdout).get("candles", [])
                    if candles:
                        self._earliest[key] = float(candles[0]["time"])
                except Exception:  # noqa: BLE001 — unknown means not covered
                    pass
        return self._earliest[key]

    def finest_reaching(self, pair: str, t: float) -> int | None:
        for interval in self.INTERVALS:
            first = self.earliest(pair, interval)
            if first is not None and first <= t:
                return interval
        return None


def load_funding(symbol: str, offline: bool) -> tuple[dict[int, float], float]:
    """Hourly relative funding rates {hour_ts: rate} and the mean (fallback)."""
    path = CACHE / f"funding_{symbol}.json"
    rows = None
    if path.exists():
        rows = json.loads(path.read_text())
    elif not offline:
        out = subprocess.run(
            ["kraken", "futures", "historical-funding-rates", symbol, "-o", "json"],
            capture_output=True, text=True, timeout=120, check=False)
        data = json.loads(out.stdout)
        rows = data.get("rates", data) if isinstance(data, dict) else data
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows))
    rates = {}
    for r in rows or []:
        ts = int(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp())
        rates[ts] = float(r["relativeFundingRate"])
    mean = statistics.fmean(rates.values()) if rates else 0.0
    return rates, mean


def load_specs():
    raw = {i["symbol"]: i for i in json.loads(
        (ROOT / "tests" / "fixtures" / "futures_instruments.json").read_text()
    )["instruments"]}
    return {asset: parse_instrument(raw[sym], asset) for asset, sym in TRADEABLE.items()}


# --------------------------------------------------------------------------
# Simulated trade (mirrors engine.py / manager.py)
# --------------------------------------------------------------------------

@dataclass
class Leg:
    price: float
    size: float
    filled: bool = False
    fill_price: float = 0.0


@dataclass
class SimTrade:
    signal_id: int
    asset: CanonicalAsset
    symbol: str
    direction: Direction
    t_signal: float
    t_place: int
    entry_prices: tuple
    tps: tuple
    stop: float
    spec: object
    be_after: int
    entries: list = field(default_factory=list)
    ladder: list = field(default_factory=list)
    ladder_active_from: int = 0
    position: float = 0.0
    entry_cost: float = 0.0           # sum(size * price) of entry fills
    entered_size: float = 0.0
    realized: float = 0.0             # price P&L of exits
    fees: float = 0.0
    funding: float = 0.0
    breakeven: bool = False
    highest_tp: int = 0
    ended: str | None = None          # outcome once terminal
    t_end: float | None = None
    last_price: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.direction is Direction.LONG

    @property
    def avg_entry(self) -> float:
        return self.entry_cost / self.entered_size if self.entered_size else 0.0

    @property
    def net(self) -> float:
        return self.realized - self.fees - self.funding

    @property
    def active(self) -> bool:
        return self.ended is None

    @property
    def has_fills(self) -> bool:
        return self.entered_size > 0

    # -- fills -------------------------------------------------------------

    def _fill_entry(self, leg: Leg, price: float, *, resting: bool) -> None:
        leg.filled, leg.fill_price = True, price
        self.position += leg.size
        self.entered_size += leg.size
        self.entry_cost += leg.size * price
        maker = COST_MODEL == "favourable" and resting
        self.fees += leg.size * price * (MAKER_FEE if maker else TAKER_FEE)
        FILLS[("entry, resting limit" if resting else "entry, marketable at placement",
               "maker" if maker else "taker")] += 1

    def _exit(self, size: float, price: float, kind: str) -> None:
        sign = 1 if self.is_long else -1
        self.realized += sign * (price - self.avg_entry) * size
        self.fees += size * price * TAKER_FEE
        FILLS[(kind, "taker")] += 1
        self.position = max(0.0, self.position - size)
        if self.position < 1e-12:
            self.position = 0.0

    def _end(self, outcome: str, t: float) -> None:
        # _finalise_close cancels every leftover order, entries included.
        self.ended, self.t_end = outcome, t

    def _adverse(self, price: float, closing: bool, stop: bool = False) -> float:
        """Slippage against us. Closing a long sells (lower), opening buys."""
        if COST_MODEL == "favourable":
            slip = FAVOURABLE_STOP_SLIPPAGE if stop else 0.0
        else:
            slip = SLIPPAGE
        buy = (not closing) == self.is_long
        return price * (1 + slip) if buy else price * (1 - slip)

    def _place_ladder(self, t_next: int) -> None:
        """place_take_profits: split the ACTUAL position, 25% per level."""
        parts = split_take_profits(D(str(round(self.position, 10))), self.spec,
                                   levels=len(self.tps))
        self.ladder = [Leg(float(p), float(s)) for p, s in zip(self.tps, parts)]
        self.ladder_active_from = t_next

    def _resize_ladder(self) -> None:
        """resize_take_profits_to_position: re-split the position over open legs."""
        open_legs = [leg for leg in self.ladder if not leg.filled]
        if not open_legs:
            return
        parts = split_take_profits(D(str(round(self.position, 10))), self.spec,
                                   levels=len(open_legs))
        for leg, size in zip(open_legs, parts):
            leg.size = float(size)
        for leg in open_legs[len(parts):]:
            leg.size = 0.0

    # -- one candle ----------------------------------------------------------

    def step(self, t: int, o: float, h: float, low: float, c: float,
             funding: tuple[dict, float]) -> None:
        if not self.active:
            return
        self.last_price = c

        # Funding at the top of each hour on the open position.
        if self.position and t % 3600 == 0:
            rates, mean = funding
            rate = rates.get(t, mean)
            sign = 1 if self.is_long else -1
            self.funding += sign * self.position * o * rate

        # 1. Stop first (pessimistic ordering).
        if self.position and self._stop_hit(o, low, h):
            return self._stopped(t, o)

        # 2. Entry expiry (reconciler sweep, brief 12).
        if t >= self.t_place + ENTRY_EXPIRY_S:
            for leg in self.entries:
                if not leg.filled:
                    leg.size = 0.0
            if not self.has_fills:
                return self._end("NEVER_FILLED", t)

        # 3. Resting entries.
        filled_now = False
        for leg in self.entries:
            if leg.filled or leg.size == 0:
                continue
            touched = low <= leg.price if self.is_long else h >= leg.price
            if touched:
                had_position = self.position > 0
                self._fill_entry(leg, leg.price, resting=True)
                filled_now = True
                if not had_position and not self.ladder:
                    self._place_ladder(t + 60)
                elif self.ladder:
                    self._resize_ladder()
        if filled_now and self._stop_hit(o, low, h, same_bar_after_fill=True):
            return self._stopped(t, o)

        # 4. TP ladder (not in the minute it was placed).
        if self.position and self.ladder and t >= self.ladder_active_from:
            for i, leg in enumerate(self.ladder, start=1):
                if leg.filled or leg.size <= 0:
                    continue
                hit = h >= leg.price if self.is_long else low <= leg.price
                if not hit:
                    break                    # ladder fills in order
                leg.filled = True
                size = min(leg.size, self.position)
                self._exit(size, self._adverse(leg.price, closing=True),
                           "take-profit trigger")
                self.highest_tp = i
                if self.position == 0:
                    break
            # Breakeven: authorised by a confirmed TP fill (engine: TP2).
            if (not self.breakeven and self.position
                    and self.highest_tp >= self.be_after):
                be = round_price_to_tick(D(str(self.avg_entry)), self.spec.tick_size,
                                         round_up=not self.is_long)
                self.stop = float(be)
                self.breakeven = True
            if self.position == 0:
                return self._end("TP_LADDER_COMPLETE"
                                 if self.highest_tp >= len(self.ladder)
                                 else "TP_LADDER_FLAT", t)
            if self.highest_tp >= len(self.ladder) and self.position:
                # manager step 6: TP4 filled — close whatever remains.
                self._exit(self.position, self._adverse(c, closing=True),
                           "market close of remainder after last TP")
                return self._end("TP_LADDER_COMPLETE", t)

    def _stop_hit(self, o, low, h, same_bar_after_fill=False) -> bool:
        return low <= self.stop if self.is_long else h >= self.stop

    def _stopped(self, t: int, o: float) -> None:
        gap = (o < self.stop) if self.is_long else (o > self.stop)
        base = o if gap else self.stop
        price = self._adverse(base, closing=True, stop=True)
        liq = self.avg_entry * (1 - (1 / float(LEVERAGE) - MAINTENANCE) * (
            1 if self.is_long else -1))
        if (self.is_long and price < liq) or (not self.is_long and price > liq):
            price = liq
            self.notes.append("liquidated (gap beyond liquidation estimate)")
        self._exit(self.position, price, "stop exit")
        self._end("BREAKEVEN_STOP" if self.breakeven else "STOP", t)

    # -- channel instructions ------------------------------------------------

    def market_close(self, t: int, price: float, why: str) -> None:
        if self.position:
            self._exit(self.position, self._adverse(price, closing=True),
                       "market close by channel")
        self._end(why if self.has_fills else "CANCELLED_BY_CHANNEL", t)


# --------------------------------------------------------------------------
# Channel result posts (what the channel CLAIMS happened)
# --------------------------------------------------------------------------

import re  # noqa: E402

_TP_RE = re.compile(r"take-?profit target (\d)", re.I)
_ALL_TP_RE = re.compile(r"all take-?profit targets|hits all targets|all targets achieved", re.I)
_STOP_RE = re.compile(r"stop ?loss ⛔|stop target hit|stoploss", re.I)
_ENTRY_RE = re.compile(r"entry (?:target )?\d ✅|all entr(?:y targets|ies) achieved", re.I)
_PCT_RE = re.compile(r"(profit|loss):\s*([\d.]+)%", re.I)
_CLOSED_RE = re.compile(r"closed|cancel", re.I)


@dataclass
class Claim:
    highest_tp: int = 0
    stopped: bool = False
    entered: bool = False
    closed_other: bool = False
    profit_pct: float = 0.0
    loss_pct: float = 0.0

    @property
    def outcome(self) -> str:
        if self.highest_tp and self.stopped:
            return f"TP{self.highest_tp} then stop"
        if self.highest_tp:
            return f"TP{self.highest_tp}"
        if self.stopped:
            return "stop"
        if self.closed_other:
            return "closed/cancelled"
        if self.entered:
            return "entered, no result"
        return "no posts"


def read_claim(claim: Claim, text: str) -> None:
    m = _TP_RE.search(text)
    if m:
        claim.highest_tp = max(claim.highest_tp, int(m.group(1)))
    if _ALL_TP_RE.search(text):
        claim.highest_tp = max(claim.highest_tp, 4)
    if _STOP_RE.search(text):
        claim.stopped = True
    if _ENTRY_RE.search(text):
        claim.entered = True
    if _CLOSED_RE.search(text) and not m:
        claim.closed_other = True
    for kind, pct in _PCT_RE.findall(text):
        if kind.lower() == "profit":
            claim.profit_pct = max(claim.profit_pct, float(pct))
        else:
            claim.loss_pct = max(claim.loss_pct, float(pct))


# --------------------------------------------------------------------------
# The replay
# --------------------------------------------------------------------------

@dataclass
class SignalRecord:
    msg_id: int
    t: float
    asset: CanonicalAsset
    direction: Direction | None
    verdict: str                      # OPENED, NOT_COVERED*, or a refusal reason
    detail: str = ""
    trade: SimTrade | None = None
    signal_leverage: float | None = None


class Replay:
    def __init__(self, offline: bool, be_after: int, quiet: bool = False) -> None:
        self.offline = offline
        self.be_after = be_after
        self.quiet = quiet
        self.specs = load_specs()
        self.candles = {sym: Candles(sym, offline) for sym in TRADEABLE.values()}
        self.funding = {sym: load_funding(sym, offline) for sym in TRADEABLE.values()}
        self.spot = SpotProbe(offline)
        self.records: list[SignalRecord] = []
        self.trades: list[SimTrade] = []
        self.by_origin: dict[int, SimTrade] = {}
        self.fingerprints: set[str] = set()
        self.cursor: dict[str, int] = {}             # last minute simulated per symbol
        self.instructions = Counter()

    # -- clock -------------------------------------------------------------

    def advance(self, until: float) -> None:
        """Run every active trade's candles up to (not including) `until`."""
        by_symbol = defaultdict(list)
        for tr in self.trades:
            if tr.active:
                by_symbol[tr.symbol].append(tr)
        end = int(until // 60) * 60
        for sym, trades in by_symbol.items():
            start = min(max(tr.t_place, self.cursor.get(sym, 0)) for tr in trades)
            src, fund = self.candles[sym], self.funding[sym]
            m = start
            while m < end and any(tr.active for tr in trades):
                bar = src.get(m)
                if bar is not None:
                    for tr in trades:
                        if tr.active and m >= tr.t_place:
                            tr.step(m, *bar, fund)
                m += 60
            self.cursor[sym] = end

    def price_at(self, symbol: str, t: float) -> float | None:
        m = int(t // 60) * 60
        for i in range(0, 30):
            bar = self.candles[symbol].get(m + i * 60)
            if bar is not None:
                return bar[0]
        return None

    def active_on(self, symbol: str) -> list[SimTrade]:
        return [tr for tr in self.trades if tr.active and tr.symbol == symbol]

    # -- signals -----------------------------------------------------------

    def on_signal(self, msg, parsed, t: float) -> None:
        asset = parsed.asset
        rec = SignalRecord(msg["id"], t, asset, parsed.direction, "",
                           signal_leverage=float(parsed.leverage) if parsed.leverage else None)
        self.records.append(rec)
        symbol = TRADEABLE[asset]
        spec = self.specs[asset]

        # Coverage first: without candles nothing below can be evaluated honestly.
        if not self.candles[symbol].covers(t):
            interval = self.spot.finest_reaching(SPOT_PAIR[symbol], t)
            rec.verdict = "NOT_COVERED"
            rec.detail = (f"no PF_ 1m candles; finest Kraken spot candle reaching "
                          f"back that far: {interval} min" if interval
                          else "no PF_ 1m candles and no spot candles")
            return

        # Admission, in admission.admit() order.
        if parsed.parse_errors:
            rec.verdict, rec.detail = "MISSING_FIELDS", "; ".join(parsed.parse_errors)
            return
        if parsed.message_type is not MessageType.NEW_SIGNAL or parsed.direction is None:
            rec.verdict = "NOT_EXECUTABLE_TYPE"
            return
        if parsed.stop_loss is None or not parsed.entry_range or not parsed.take_profits:
            rec.verdict = "MISSING_FIELDS"
            return
        fp = fingerprint_for(parsed)
        if fp and fp in self.fingerprints:
            rec.verdict = "DUPLICATE_SIGNAL"
            return
        active = self.active_on(symbol)
        if any(tr.direction is not parsed.direction for tr in active):
            rec.verdict = "OPPOSITE_DIRECTION_ACTIVE"
            return
        if len(active) >= MAX_TRADES_PER_SYMBOL:
            rec.verdict = "SYMBOL_ALREADY_ACTIVE"
            return

        # plan_entries: stop vs liquidation (with siblings), then sizing.
        is_short = parsed.direction is Direction.SHORT
        entries = tuple(round_price_to_tick(p, spec.tick_size, round_up=is_short)
                        for p in parsed.entry_range)
        stop = round_price_to_tick(parsed.stop_loss, spec.tick_size, round_up=is_short)
        pool = entries + tuple(D(str(p)) for tr in active for p in tr.entry_prices)
        stops = [stop] + [D(str(tr.stop)) for tr in active]
        try:
            for i, st in enumerate(stops):
                try:
                    safe = max_leverage_for_stop(pool, st, is_short=is_short,
                                                 buffer_percent=STOP_LIQ_BUFFER_PCT)
                except SizingError as exc:
                    if i > 0 and exc.reason == "STOP_WRONG_SIDE":
                        continue
                    raise
                if safe < LEVERAGE:
                    rec.verdict = "STOP_BEYOND_LIQUIDATION"
                    rec.detail = f"needs <= {safe}x"
                    return
            sizes = []
            for price, margin in zip(entries, split_entry_allocations(
                    TRADE_MARGIN, len(entries))):
                sizes.append(calculate_size(
                    margin_usd=margin, leverage=LEVERAGE, price=price, spec=spec,
                    max_deviation_percent=MAX_SIZING_DEVIATION_PCT).size)
        except SizingError as exc:
            rec.verdict, rec.detail = exc.reason, exc.detail
            return

        # open_trade: stale-entry check against the price when the order goes in.
        t_place = int(t // 60) * 60 + 60
        mark = self.price_at(symbol, t_place)
        if mark is None:
            rec.verdict = "NOT_COVERED"
            rec.detail = "no candle at placement"
            return
        stale, dev = is_entry_stale(D(str(mark)), entries, MAX_ENTRY_DEVIATION_PCT)
        if stale:
            rec.verdict, rec.detail = "STALE_ENTRY", f"{dev:.2f}% beyond range"
            return

        tr = SimTrade(
            signal_id=msg["id"], asset=asset, symbol=symbol,
            direction=parsed.direction, t_signal=t, t_place=t_place,
            entry_prices=tuple(float(p) for p in entries),
            tps=tuple(float(round_price_to_tick(p, spec.tick_size,
                                                round_up=not is_short))
                      for p in parsed.take_profits),
            stop=float(stop), spec=spec, be_after=self.be_after,
        )
        for price, size in zip(entries, sizes):
            leg = Leg(float(price), float(size))
            tr.entries.append(leg)
            # A limit already through the market fills at once, as a taker.
            marketable = (mark <= leg.price) if tr.is_long else (mark >= leg.price)
            if marketable:
                had = tr.position > 0
                # A limit never fills worse than its own price.
                px = tr._adverse(mark, closing=False)
                px = min(px, leg.price) if tr.is_long else max(px, leg.price)
                tr._fill_entry(leg, px, resting=False)
                if not had:
                    tr._place_ladder(t_place + 60)
                else:
                    tr._resize_ladder()
        self.trades.append(tr)
        self.by_origin[msg["id"]] = tr
        if fp:
            self.fingerprints.add(fp)
        rec.verdict, rec.trade = "OPENED", tr

    # -- channel instructions (pipeline._act) ---------------------------------

    def _resolve(self, msg, asset, replies) -> tuple[list | None, str]:
        """pipeline._resolve_target: reply chain (4 hops), else symbol."""
        cur, hops = msg["reply_to"], 0
        while cur is not None and hops < 4:
            tr = self.by_origin.get(cur)
            if tr is not None:
                return ([tr] if tr.active else []), "reply"
            cur = replies.get(cur)
            hops += 1
        if msg["reply_to"] is not None:
            return [], "reply leads to no trade"
        if asset is not None and asset in TRADEABLE:
            return self.active_on(TRADEABLE[asset]), "symbol"
        return None, "ambiguous"

    def on_instruction(self, msg, parsed, cls, t: float, replies) -> None:
        t_exec = int(t // 60) * 60 + 60
        action = cls.action
        if action is MessageAction.UPDATE_STOP:
            trades, _ = self._resolve(msg, cls.asset, replies)
            if not trades or len(trades) != 1:
                self.instructions["stop update: no unique target"] += 1
                return
            tr = trades[0]
            if cls.asset is not None and cls.asset is not tr.asset:
                self.instructions["stop update: asset mismatch"] += 1
                return
            new = float(round_price_to_tick(cls.new_stop, tr.spec.tick_size,
                                            round_up=not tr.is_long))
            tighter = new > tr.stop if tr.is_long else new < tr.stop
            mark = self.price_at(tr.symbol, t_exec)
            wrong_side = mark is None or (new >= mark if tr.is_long else new <= mark)
            if new == tr.stop:
                self.instructions["stop update: already there"] += 1
            elif not tighter or wrong_side:
                self.instructions["stop update: refused (widen/wrong side)"] += 1
            else:
                tr.stop = new
                self.instructions["stop update: applied"] += 1
            return

        if action is MessageAction.CANCEL_TRADE:
            asset = cls.asset or parsed.asset
            trades, _ = self._resolve(msg, asset, replies)
            if not trades or len(trades) != 1:
                self.instructions["cancel: no unique target"] += 1
                return
            tr = trades[0]
            if tr.has_fills:
                self.instructions["cancel: trade has fills -> human"] += 1
                return
            tr._end("CANCELLED_BY_CHANNEL", t_exec)
            self.instructions["cancel: applied"] += 1
            return

        if action is MessageAction.CLOSE_TRADE:
            scope = cls.close_scope
            asset, direction = cls.asset, cls.direction
            live = [tr for tr in self.trades if tr.active]
            if scope is MessageType.CLOSE_REPLY:
                targets = [tr for tr in live if tr.signal_id == msg["reply_to"]] or None
            elif scope is MessageType.CLOSE_SYMBOL:
                if asset not in TRADEABLE:
                    self.instructions["close: asset not tradeable"] += 1
                    return
                targets = [tr for tr in live if tr.symbol == TRADEABLE[asset]]
                if targets and direction is not None:
                    targets = [tr for tr in targets if tr.direction is direction]
                    if not targets:
                        self.instructions["close: direction mismatch -> human"] += 1
                        return
            elif scope is MessageType.CLOSE_DIRECTION:
                targets = [tr for tr in live if tr.direction is direction]
            elif scope is MessageType.CLOSE_ALL:
                targets = live
            else:
                targets = None
            if targets is None:
                self.instructions["close: ambiguous -> human"] += 1
                return
            if not targets:
                self.instructions["close: nothing to close"] += 1
                return
            for tr in targets:
                price = self.price_at(tr.symbol, t_exec) or tr.last_price
                tr.market_close(t_exec, price, "CLOSED_BY_CHANNEL")
            self.instructions["close: applied"] += 1

    # -- driver ------------------------------------------------------------

    def run(self, messages) -> None:
        replies = {m["id"]: m["reply_to"] for m in messages}
        self.claims: dict[int, Claim] = {}
        signal_ids: set[int] = set()
        total = len(messages)
        for n, msg in enumerate(messages):
            if not self.quiet and n % 2000 == 0:
                reqs = sum(c.requests for c in self.candles.values())
                print(f"  ... {n}/{total} messages, {reqs} candle requests",
                      file=sys.stderr, flush=True)
            if not msg["text"]:
                continue
            t = datetime.fromisoformat(msg["date"]).timestamp()
            parsed = parse_message(channel_id=-1, message_id=msg["id"], text=msg["text"],
                                   timestamp=t, reply_to_message_id=msg["reply_to"])
            cls = classify_message(parsed, media=msg["media"])

            # The channel's own result posts, attributed to the signal they reply to.
            if msg["reply_to"] is not None:
                root, hops = msg["reply_to"], 0
                while root not in signal_ids and replies.get(root) and hops < 4:
                    root, hops = replies[root], hops + 1
                if root in signal_ids:
                    read_claim(self.claims.setdefault(root, Claim()), msg["text"])

            is_signal = (cls.message_class.value == "NEW_SIGNAL"
                         and parsed.asset in TRADEABLE)
            is_instruction = cls.action in (MessageAction.UPDATE_STOP,
                                            MessageAction.CLOSE_TRADE,
                                            MessageAction.CANCEL_TRADE)
            if not (is_signal or is_instruction):
                continue
            self.advance(t)
            if is_signal:
                signal_ids.add(msg["id"])
                self.on_signal(msg, parsed, t)
            else:
                self.on_instruction(msg, parsed, cls, t, replies)
        self.advance(time.time() - 120)
        for tr in self.trades:
            if tr.active:
                tr.notes.append("still open at end of data")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def quarter(t: float) -> str:
    d = datetime.fromtimestamp(t, UTC)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def max_drawdown(trades) -> tuple[float, float]:
    """(max drawdown $, peak equity before it) on realised equity by close time."""
    eq = peak = dd = 0.0
    for tr in sorted(trades, key=lambda x: x.t_end):
        eq += tr.net
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd, peak


def summarise(trades) -> dict:
    done = [tr for tr in trades if not tr.active and tr.has_fills]
    nets = [tr.net for tr in done]
    return {
        "filled": len(done),
        "wins": sum(1 for x in nets if x > 0),
        "losses": sum(1 for x in nets if x <= 0),
        "net": sum(nets),
        "per_trade": statistics.fmean(nets) if nets else 0.0,
    }


def fmt_money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def report(rp: Replay, variant: Replay | None) -> str:
    out: list[str] = []
    w = out.append
    recs = rp.records
    trades = rp.trades
    opened = [r for r in recs if r.verdict == "OPENED"]
    not_cov = [r for r in recs if r.verdict == "NOT_COVERED"]
    refused = [r for r in recs if r.verdict not in ("OPENED", "NOT_COVERED")]
    never = [tr for tr in trades if tr.ended in ("NEVER_FILLED",)]
    cancelled_unfilled = [tr for tr in trades
                          if tr.ended == "CANCELLED_BY_CHANNEL" and not tr.has_fills]
    still_open = [tr for tr in trades if tr.active]
    closed = [tr for tr in trades if not tr.active and tr.has_fills]
    wins = [tr for tr in closed if tr.net > 0]
    losses = [tr for tr in closed if tr.net <= 0]
    be = [tr for tr in closed if tr.ended == "BREAKEVEN_STOP"]

    w("BACKTEST — channel signals under the bot's rules")
    w("=" * 70)
    first = min((r.t for r in recs), default=0)
    last = max((r.t for r in recs), default=0)
    w(f"Signals: NEW_SIGNAL on a tradeable asset (BTC ETH SOL BNB XRP DOGE), "
      f"{datetime.fromtimestamp(first, UTC):%Y-%m-%d} .. "
      f"{datetime.fromtimestamp(last, UTC):%Y-%m-%d}")
    w("")
    w(f"  signals total                     {len(recs):5d}")
    w(f"  NOT COVERED by candle data        {len(not_cov):5d}")
    w(f"  refused by a rule                 {len(refused):5d}")
    w(f"  opened (entries placed)           {len(opened):5d}")
    w(f"    entries never filled (24h)      {len(never):5d}")
    w(f"    cancelled by channel, unfilled  {len(cancelled_unfilled):5d}")
    w(f"    still open at end of data       {len(still_open):5d}")
    w(f"    closed with a position          {len(closed):5d}")
    w(f"      wins  (net P&L > 0)           {len(wins):5d}")
    w(f"      losses (net P&L <= 0)         {len(losses):5d}")
    w(f"      of which breakeven-stop exits {len(be):5d}  "
      f"(after TP{rp.be_after}; net {fmt_money(sum(t.net for t in be))})")
    w("")
    w("Not covered, by reason:")
    for why, n in Counter(r.detail for r in not_cov).most_common():
        w(f"  {n:5d}  {why}")
    w("")
    w("Refused, by rule:")
    for why, n in Counter(r.verdict for r in refused).most_common():
        w(f"  {n:5d}  {why}")
    w("")
    w("Exit types (closed with a position):")
    for why, n in Counter(tr.ended for tr in closed).most_common():
        sub = [tr for tr in closed if tr.ended == why]
        w(f"  {n:5d}  {why:22s} net {fmt_money(sum(t.net for t in sub)):>12s}")
    w("")
    gross = sum(tr.realized for tr in closed)
    fees = sum(tr.fees for tr in closed)
    fund = sum(tr.funding for tr in closed)
    net = sum(tr.net for tr in closed)
    dd, peak = max_drawdown(closed)
    w("P&L at our sizing ($30 margin, 10x, $300 notional per signal)")
    w(f"  gross price P&L                {fmt_money(gross):>12s}")
    fee_label = "fees (maker + taker)" if COST_MODEL == "favourable" else "taker fees"
    w(f"  {fee_label:29s} {fmt_money(-fees):>12s}")
    w(f"  funding                        {fmt_money(-fund):>12s}")
    w(f"  NET                            {fmt_money(net):>12s}")
    w(f"  per closed trade (expected)    {fmt_money(net / len(closed) if closed else 0):>12s}")
    w(f"  per opened signal              {fmt_money(net / len(opened) if opened else 0):>12s}")
    w(f"  win rate                       {len(wins) / len(closed) * 100 if closed else 0:11.1f}%")
    if wins and losses:
        w(f"  average win / average loss     {fmt_money(statistics.fmean(t.net for t in wins))}"
          f" / {fmt_money(statistics.fmean(t.net for t in losses))}")
    w(f"  max drawdown (realised)        {fmt_money(-dd):>12s}   (equity peak before it "
      f"{fmt_money(peak)})")
    liq = sum(1 for tr in closed if any("liquidated" in n for n in tr.notes))
    if liq:
        w(f"  stops gapped past liquidation  {liq:12d}")
    if still_open:
        unreal = 0.0
        for tr in still_open:
            sign = 1 if tr.is_long else -1
            unreal += tr.net + sign * (tr.last_price - tr.avg_entry) * tr.position
        w(f"  still open (unrealised, excl.) {fmt_money(unreal):>12s}  on {len(still_open)} trade(s)")
    w("")
    w("By asset (closed with a position):")
    w(f"  {'asset':6s} {'signals':>7s} {'opened':>6s} {'closed':>6s} {'wins':>5s} "
      f"{'loss':>5s} {'net':>11s} {'per trade':>10s}")
    for asset in TRADEABLE:
        sig = [r for r in recs if r.asset is asset]
        op = [r for r in sig if r.verdict == "OPENED"]
        s = summarise([tr for tr in trades if tr.asset is asset])
        w(f"  {asset.value:6s} {len(sig):7d} {len(op):6d} {s['filled']:6d} {s['wins']:5d} "
          f"{s['losses']:5d} {fmt_money(s['net']):>11s} {fmt_money(s['per_trade']):>10s}")
    w("")
    w("By quarter of the signal (closed with a position):")
    w(f"  {'quarter':8s} {'signals':>7s} {'n/cov':>5s} {'opened':>6s} {'closed':>6s} "
      f"{'wins':>5s} {'loss':>5s} {'net':>11s}")
    for q in sorted({quarter(r.t) for r in recs}):
        sig = [r for r in recs if quarter(r.t) == q]
        s = summarise([tr for tr in trades if quarter(tr.t_signal) == q])
        w(f"  {q:8s} {len(sig):7d} {sum(r.verdict == 'NOT_COVERED' for r in sig):5d} "
          f"{sum(r.verdict == 'OPENED' for r in sig):6d} {s['filled']:6d} {s['wins']:5d} "
          f"{s['losses']:5d} {fmt_money(s['net']):>11s}")
    w("")
    w("Channel instructions the bot would have acted on:")
    for k, n in sorted(rp.instructions.items()):
        w(f"  {n:5d}  {k}")
    w("")

    # -- the channel's own claims ------------------------------------------
    w("Compared with the channel's own result posts (same signals, opened by us)")
    pairs = [(tr, rp.claims.get(tr.signal_id, Claim())) for tr in trades
             if not tr.active]
    claim_counts = Counter(c.outcome for _, c in pairs)
    w("  what the channel posted about these signals:")
    for k, n in claim_counts.most_common():
        w(f"    {n:5d}  {k}")
    claimed_tp = [c for _, c in pairs if c.highest_tp]
    claimed_stop_only = [c for _, c in pairs if c.stopped and not c.highest_tp]
    decided = len(claimed_tp) + len(claimed_stop_only)
    w(f"  channel 'win rate' (any TP claimed, of TP-or-stop posts): "
      f"{len(claimed_tp) / decided * 100 if decided else 0:.1f}% "
      f"({len(claimed_tp)}/{decided})")
    sim_tp = [tr for tr, _ in pairs if tr.highest_tp]
    sim_dec = [tr for tr, _ in pairs if tr.has_fills]
    w(f"  our sim, same signals: reached >= TP1 on {len(sim_tp)}/{len(sim_dec)} filled "
      f"trades ({len(sim_tp) / len(sim_dec) * 100 if sim_dec else 0:.1f}%); net-positive "
      f"{len(wins)}/{len(closed)}")
    agree = Counter()
    for tr, c in pairs:
        if not (c.highest_tp or c.stopped):
            continue
        ch = "TP" if c.highest_tp else "stop"
        if not tr.has_fills:
            us = "never filled/cancelled"
        else:
            us = "TP" if tr.highest_tp else "stop/close w/o TP"
        agree[(ch, us)] += 1
    w("  channel claim  vs  our simulated result:")
    for (ch, us), n in sorted(agree.items()):
        w(f"    {n:5d}  channel {ch:5s} | sim {us}")
    tp_gap = Counter()
    for tr, c in pairs:
        if c.highest_tp and tr.has_fills:
            tp_gap[(c.highest_tp, tr.highest_tp)] += 1
    over = sum(n for (ch, us), n in tp_gap.items() if ch > us)
    same = sum(n for (ch, us), n in tp_gap.items() if ch == us)
    under = sum(n for (ch, us), n in tp_gap.items() if ch < us)
    w(f"  where the channel claimed a TP and we filled: channel's TP higher than ours "
      f"{over}, same {same}, lower {under}")
    # Claimed % converted to our sizing: profit% is on margin at the signal's own
    # leverage; price move = pct / lev; our $ = $30 x 10 x move. It treats the
    # WHOLE position as closed at the highest TP claimed, as the channel's figure
    # does, so it is an upper bound on what the claim implies, not a ladder.
    claimed_usd = 0.0
    for tr, c in pairs:
        rec = next(r for r in recs if r.msg_id == tr.signal_id)
        lev = rec.signal_leverage or 10.0
        if c.highest_tp:
            claimed_usd += float(TRADE_MARGIN) * float(LEVERAGE) * c.profit_pct / lev / 100
        elif c.stopped:
            claimed_usd -= float(TRADE_MARGIN) * float(LEVERAGE) * c.loss_pct / lev / 100
    w(f"  channel's posted Profit/Loss % on these signals, restated at our sizing "
      f"(no fees): {fmt_money(claimed_usd)}  vs our net {fmt_money(net)}")
    w("")
    if variant is not None:
        vc = [tr for tr in variant.trades if not tr.active and tr.has_fills]
        vnet = sum(tr.net for tr in vc)
        vw = sum(1 for tr in vc if tr.net > 0)
        vdd, _ = max_drawdown(vc)
        w(f"Comparison only — breakeven after TP1 instead of the engine's TP2: "
          f"net {fmt_money(vnet)}, {vw}/{len(vc)} wins, "
          f"{sum(1 for tr in vc if tr.ended == 'BREAKEVEN_STOP')} breakeven exits, "
          f"max drawdown {fmt_money(-vdd)}")
        w("")
    w("Assumptions:")
    for a in ASSUMPTIONS:
        w(f"  - {a}")
    reqs = sum(c.requests for c in rp.candles.values())
    w("")
    w(f"Data: Kraken Futures public charts API (1m trade candles), cache {CACHE}; "
      f"{reqs} new chunk request(s) this run.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="use cached candles only; never fetch")
    ap.add_argument("--corpus", default=str(ROOT / "corpus" / "channel_history.jsonl"))
    ap.add_argument("--out", default=str(CACHE / "report.txt"))
    ap.add_argument("--fee-mult", type=float, default=1.0,
                    help="scale the taker fee (stress test)")
    ap.add_argument("--slip-mult", type=float, default=1.0,
                    help="scale the slippage (stress test)")
    ap.add_argument("--costs", choices=("default", "favourable"), default="default",
                    help="favourable: maker fee on resting limit entries only, "
                         "slippage 0.02%% on stop exits only")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD: replay only messages posted on/after this "
                         "date, as if the bot had started then")
    args = ap.parse_args(argv)

    global TAKER_FEE, SLIPPAGE, COST_MODEL
    COST_MODEL = args.costs
    if COST_MODEL == "favourable":
        ASSUMPTIONS.append(
            f"FAVOURABLE COSTS: maker {MAKER_FEE:.2%} on entries that rest on the "
            f"book and fill later; taker {TAKER_FEE:.2%} on every other fill "
            "(entries marketable at placement, take-profit triggers, stop exits, "
            f"market closes). Slippage {FAVOURABLE_STOP_SLIPPAGE:.2%} on stop exits "
            "only; none elsewhere. Supersedes the fee and slippage lines above.")
    TAKER_FEE *= args.fee_mult
    SLIPPAGE *= args.slip_mult
    if args.fee_mult != 1.0 or args.slip_mult != 1.0:
        ASSUMPTIONS.append(
            f"STRESS RUN: taker fee {TAKER_FEE:.4%} (x{args.fee_mult:g}), "
            f"slippage {SLIPPAGE:.4%} (x{args.slip_mult:g}).")

    messages = [json.loads(line) for line in open(args.corpus)]
    messages.sort(key=lambda m: (m["date"], m["id"]))
    if args.since:
        messages = [m for m in messages if str(m["date"]) >= args.since]
        ASSUMPTIONS.append(
            f"WINDOW: only messages posted on/after {args.since}; the replay "
            "starts flat then (no earlier trades occupy symbols).")

    print("replaying (engine rules: breakeven after TP2) ...", file=sys.stderr)
    main_run = Replay(args.offline, be_after=2)
    main_run.run(messages)
    main_fills = Counter(FILLS)
    print("replaying comparison variant (breakeven after TP1) ...", file=sys.stderr)
    variant = Replay(True, be_after=1, quiet=True)      # cache is warm by now
    variant.run(messages)

    text = report(main_run, variant)
    if COST_MODEL == "favourable":
        text += "\n\nFills by type and fee (main run, engine rules):\n" + "\n".join(
            f"  {n:6d}  {kind:42s} {fee}"
            for (kind, fee), n in sorted(main_fills.items()))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n")
    rows = [{
        "signal_id": r.msg_id, "date": datetime.fromtimestamp(r.t, UTC).isoformat(),
        "asset": r.asset.value, "direction": r.direction.value if r.direction else None,
        "verdict": r.verdict, "detail": r.detail,
        **({"outcome": r.trade.ended, "net": round(r.trade.net, 4),
            "closed_at": (datetime.fromtimestamp(r.trade.t_end, UTC).isoformat()
                          if r.trade.t_end else None),
            "highest_tp": r.trade.highest_tp, "fees": round(r.trade.fees, 4),
            "funding": round(r.trade.funding, 4), "notes": r.trade.notes,
            "claim": main_run.claims.get(r.msg_id, Claim()).outcome}
           if r.trade else {}),
    } for r in main_run.records]
    Path(args.out).with_suffix(".jsonl").write_text(
        "\n".join(json.dumps(x) for x in rows) + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
