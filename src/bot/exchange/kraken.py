"""Kraken adapter over kraken-cli.

Routes to `kraken futures ...` (live) or `kraken futures paper ...` (paper)
from a single code path, so the execution engine never branches on mode.

Field-name differences between the two engines are absorbed here — paper
returns `id`/`client_order_id`, live returns `order_id`/`cliOrdId` — because
that is exactly the kind of exchange detail brief 2 requires not to leak.
"""

from __future__ import annotations

from decimal import Decimal

from ..logging_setup import get_logger
from ..models import CanonicalAsset, InstrumentSpec, OrderStatus, TradingMode
from .base import (
    AccountState,
    ExchangeClient,
    ExchangeError,
    InstrumentUnavailable,
    Order,
    OrderRequest,
    OrderResult,
    Position,
)
from .cli import KrakenCli, KrakenCliError
from .specs import SYMBOL_MAP, resolve_specs

log = get_logger(__name__)


def _dec(value, default=None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


_STATUS_MAP = {
    "filled": OrderStatus.FILLED,
    "open": OrderStatus.OPEN,
    "untouched": OrderStatus.OPEN,
    "placed": OrderStatus.OPEN,
    "partiallyfilled": OrderStatus.PARTIALLY_FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def _status(raw: str | None) -> OrderStatus:
    if not raw:
        return OrderStatus.UNKNOWN
    return _STATUS_MAP.get(str(raw).lower().replace(" ", ""), OrderStatus.UNKNOWN)


class KrakenCliAdapter(ExchangeClient):
    def __init__(
        self,
        cli: KrakenCli,
        mode: TradingMode,
        assets: frozenset[CanonicalAsset],
    ) -> None:
        if not mode.touches_exchange:
            raise ValueError(
                "KrakenCliAdapter must not be used in DRY_RUN — use DryRunClient, "
                "so a dry run cannot reach the exchange even by mistake"
            )
        self.cli = cli
        self.mode = mode
        self.assets = assets
        self._specs: dict[CanonicalAsset, InstrumentSpec] = {}

    # -- command routing ----------------------------------------------------

    def _futures(self, *args: str) -> list[str]:
        """Prefix for account-scoped commands, routed by mode."""
        if self.mode is TradingMode.PAPER:
            return ["futures", "paper", *args]
        return ["futures", *args]

    def _run(self, *args: str) -> dict | list:
        try:
            return self.cli.run(*args).data
        except KrakenCliError as exc:
            raise ExchangeError(str(exc), retryable=exc.retryable) from exc

    # -- reference data -----------------------------------------------------

    def load_specs(self) -> dict[CanonicalAsset, InstrumentSpec]:
        """Resolve and cache every whitelisted instrument.

        Called at startup. `futures instruments` is public, so this works
        identically in paper and live.
        """
        data = self._run("futures", "instruments")
        instruments = data.get("instruments") if isinstance(data, dict) else data
        if not instruments:
            raise InstrumentUnavailable("instrument list was empty")
        self._specs = resolve_specs(instruments, self.assets)
        return self._specs

    def get_instrument_spec(self, asset: CanonicalAsset) -> InstrumentSpec:
        if asset not in self._specs:
            raise InstrumentUnavailable(
                f"{asset.value}: spec not loaded; call load_specs() at startup"
            )
        return self._specs[asset]

    def get_mark_price(self, symbol: str) -> Decimal:
        """Mark price — the reference our stops trigger against."""
        data = self._run("futures", "ticker", symbol)
        ticker = data.get("ticker", data) if isinstance(data, dict) else {}
        price = _dec(ticker.get("markPrice")) or _dec(ticker.get("last"))
        if price is None:
            raise ExchangeError(f"{symbol}: no mark price in ticker response")
        return price

    # -- account ------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        data = self._run(*self._futures("balance" if self.mode is TradingMode.PAPER
                                        else "accounts"))
        if self.mode is TradingMode.PAPER:
            collateral = _dec(data.get("collateral"), Decimal(0))
            available = _dec(data.get("available"), collateral) or Decimal(0)
            return AccountState(collateral=collateral, available_margin=available)

        accounts = data.get("accounts", {})
        flex = accounts.get("flex", {}) if isinstance(accounts, dict) else {}
        collateral = _dec(flex.get("portfolioValue"), Decimal(0)) or Decimal(0)
        available = _dec(flex.get("availableMargin"), collateral) or Decimal(0)
        return AccountState(collateral=collateral, available_margin=available)

    def get_open_positions(self) -> list[Position]:
        data = self._run(*self._futures("positions"))
        raw = data.get("positions", []) if isinstance(data, dict) else data
        positions = []
        for p in raw:
            size = _dec(p.get("size"), Decimal(0)) or Decimal(0)
            if size == 0:
                continue
            positions.append(
                Position(
                    symbol=p.get("symbol", ""),
                    side=str(p.get("side", "")).lower(),
                    size=size,
                    entry_price=_dec(p.get("entry_price") or p.get("price"), Decimal(0))
                    or Decimal(0),
                    mark_price=_dec(p.get("mark_price") or p.get("markPrice")),
                    liquidation_price=_dec(
                        p.get("liquidation_price") or p.get("liquidationThreshold")
                    ),
                    leverage=_dec(p.get("leverage")),
                    unrealized_pnl=_dec(p.get("unrealized_pnl") or p.get("unrealizedPnl")),
                )
            )
        return positions

    def get_position(self, symbol: str) -> Position | None:
        """The netted position for a symbol, or None when flat.

        Kraken nets per symbol, so there is at most one.
        """
        return next(
            (p for p in self.get_open_positions() if p.symbol == symbol), None
        )

    def get_open_orders(self) -> list[Order]:
        data = self._run(*self._futures("orders" if self.mode is TradingMode.PAPER
                                        else "open-orders"))
        raw = (
            data.get("open_orders") or data.get("openOrders") or []
            if isinstance(data, dict)
            else data
        )
        return [self._to_order(o) for o in raw]

    def _to_order(self, o: dict) -> Order:
        """Absorb paper-vs-live field naming."""
        size = _dec(o.get("size") or o.get("unfilledSize") or o.get("quantity"),
                    Decimal(0)) or Decimal(0)
        filled = _dec(o.get("filled_size") or o.get("filledSize"), Decimal(0)) or Decimal(0)
        return Order(
            exchange_order_id=str(o.get("id") or o.get("order_id") or o.get("orderId") or ""),
            client_order_id=o.get("client_order_id") or o.get("cliOrdId"),
            symbol=o.get("symbol", ""),
            side=str(o.get("side", "")).lower(),
            order_type=str(o.get("type") or o.get("orderType") or "").lower(),
            size=size,
            filled_size=filled,
            price=_dec(o.get("price") or o.get("limitPrice")),
            # NOTE: the paper engine omits stop_price entirely, so this is None
            # in paper even for stop orders (docs/KRAKEN_NOTES.md 4).
            trigger_price=_dec(o.get("stop_price") or o.get("stopPrice")),
            reduce_only=bool(o.get("reduce_only") or o.get("reduceOnly")),
            status=_status(o.get("status")),
        )

    # -- leverage -----------------------------------------------------------

    def get_leverage_preference(self, symbol: str) -> Decimal | None:
        data = self._run(*self._futures("leverage"))
        prefs = data.get("leverage_preferences") or data.get("leveragePreferences") or {}
        if isinstance(prefs, list):
            prefs = {
                p.get("symbol"): p.get("maxLeverage") or p.get("leverage") for p in prefs
            }
        return _dec(prefs.get(symbol))

    def set_leverage_preference(self, symbol: str, leverage: Decimal) -> Decimal:
        """Set leverage and verify by reading back.

        Study B (docs/KRAKEN_NOTES.md 1C) established two things this depends on:
        the preference persists even when a following order fails, and it then
        governs the next position opened on that symbol. So it is never
        assumed — always set, then confirmed.
        """
        current = self.get_leverage_preference(symbol)
        if current == leverage:
            return current

        self._run(*self._futures("set-leverage", symbol, str(leverage)))

        verified = self.get_leverage_preference(symbol)
        if verified != leverage:
            raise ExchangeError(
                f"{symbol}: leverage verification failed — requested {leverage}, "
                f"read back {verified}. Refusing to trade under unverified leverage."
            )
        log.info(
            f"leverage preference set and verified for {symbol}: {verified}x",
            extra={"symbol": symbol, "action": "set_leverage"},
        )
        return verified

    # -- orders -------------------------------------------------------------

    def _order_verb(self, side: str) -> list[str]:
        """`futures paper buy` vs `futures order buy` — the one shape
        difference between the two engines."""
        verb = "buy" if side == "buy" else "sell"
        if self.mode is TradingMode.PAPER:
            return self._futures(verb)
        return self._futures("order", verb)

    def place_order(self, request: OrderRequest) -> OrderResult:
        args = [
            *self._order_verb(request.side),
            request.symbol,
            str(request.size),
            "--type",
            request.order_type,
            "--client-order-id",
            request.client_order_id,
        ]
        if request.price is not None:
            args += ["--price", str(request.price)]
        if request.trigger_price is not None:
            args += ["--stop-price", str(request.trigger_price),
                     "--trigger-signal", request.trigger_signal]
        if request.reduce_only:
            args.append("--reduce-only")

        try:
            data = self.cli.run(*args).data
        except KrakenCliError as exc:
            # Do NOT retry here. The order may have reached the exchange; the
            # caller must reconcile against client_order_id first (brief 40).
            return OrderResult(
                accepted=False,
                exchange_order_id=None,
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED if not exc.retryable else OrderStatus.UNKNOWN,
                detail=str(exc),
            )

        fills = data.get("fills") or []
        filled = sum((_dec(f.get("size"), Decimal(0)) or Decimal(0)) for f in fills)
        avg = None
        if fills and filled:
            avg = sum(
                (_dec(f.get("price"), Decimal(0)) or Decimal(0))
                * (_dec(f.get("size"), Decimal(0)) or Decimal(0))
                for f in fills
            ) / filled

        return OrderResult(
            accepted=True,
            exchange_order_id=str(
                data.get("order_id") or data.get("orderId") or ""
            ) or None,
            client_order_id=request.client_order_id,
            status=_status(data.get("status")),
            filled_size=filled,
            avg_fill_price=avg,
            detail=str(data.get("message") or ""),
        )

    def cancel_order(self, exchange_order_id: str) -> bool:
        try:
            self._run(*self._futures("cancel", exchange_order_id))
            return True
        except ExchangeError:
            return False

    def amend_order(
        self,
        exchange_order_id: str,
        *,
        size: Decimal | None = None,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> bool:
        """Atomic amend — the safe stop-replacement primitive.

        Verified in paper: the order id and client order id are preserved and
        the open-order count does not change, so no window exists in which the
        position is unprotected or doubly protected.
        """
        args = [*self._futures("edit-order"), "--order-id", exchange_order_id]
        if size is not None:
            args += ["--size", str(size)]
        if price is not None:
            args += ["--price", str(price)]
        if trigger_price is not None:
            args += ["--stop-price", str(trigger_price)]
        try:
            self._run(*args)
            return True
        except ExchangeError as exc:
            log.error(
                f"amend failed for {exchange_order_id}: {exc}",
                extra={"order_id": exchange_order_id, "action": "amend_failed"},
            )
            return False
