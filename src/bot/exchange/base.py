"""Exchange abstraction.

Everything above this line speaks CanonicalAsset, Decimal and Direction.
Everything below speaks Kraken. No `PF_` symbol, no CLI flag and no Kraken
JSON shape may leak upward through this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from ..models import CanonicalAsset, InstrumentSpec, OrderPurpose, OrderStatus


class ExchangeError(Exception):
    """Any exchange-layer failure, already classified by the gateway."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class InstrumentUnavailable(ExchangeError):
    """A whitelisted asset could not be resolved to a tradeable instrument."""


@dataclass(frozen=True, slots=True)
class Position:
    """An ACTUAL exchange position. Never constructed from local intent."""

    symbol: str
    side: str  # "long" | "short"
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    unrealized_pnl: Decimal | None = None

    @property
    def is_flat(self) -> bool:
        return self.size == 0


@dataclass(frozen=True, slots=True)
class Order:
    """An ACTUAL exchange order."""

    exchange_order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    order_type: str
    size: Decimal
    filled_size: Decimal = Decimal(0)
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.UNKNOWN

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled_size


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A request to place an order. Built by the execution engine."""

    symbol: str
    side: str  # "buy" | "sell"
    size: Decimal
    order_type: str  # "limit" | "market" | "stop" | "take-profit"
    purpose: OrderPurpose
    client_order_id: str
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    trigger_signal: str = "mark"
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class OrderResult:
    accepted: bool
    exchange_order_id: str | None
    client_order_id: str
    status: OrderStatus
    filled_size: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AccountState:
    collateral: Decimal
    available_margin: Decimal
    currency: str = "USD"


class ExchangeClient(ABC):
    """The seam. Implementations: KrakenCliAdapter (live/paper), DryRunClient."""

    # -- market / reference data -------------------------------------------

    @abstractmethod
    def get_instrument_spec(self, asset: CanonicalAsset) -> InstrumentSpec: ...

    @abstractmethod
    def get_mark_price(self, symbol: str) -> Decimal: ...

    # -- account ------------------------------------------------------------

    @abstractmethod
    def get_account_state(self) -> AccountState: ...

    @abstractmethod
    def get_open_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None: ...

    @abstractmethod
    def get_open_orders(self) -> list[Order]: ...

    # -- leverage (per-symbol ACCOUNT STATE, not an order parameter) --------

    @abstractmethod
    def get_leverage_preference(self, symbol: str) -> Decimal | None: ...

    @abstractmethod
    def set_leverage_preference(self, symbol: str, leverage: Decimal) -> Decimal:
        """Set and VERIFY by reading back. Returns the verified value.

        Verification is mandatory: Study B showed a leverage preference
        persists after a subsequent order fails, and then silently governs the
        next position opened on that symbol.
        """

    # -- orders -------------------------------------------------------------

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, exchange_order_id: str) -> bool: ...

    @abstractmethod
    def amend_order(
        self,
        exchange_order_id: str,
        *,
        size: Decimal | None = None,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> bool:
        """Atomic in-place amend.

        This is the mechanism that makes stop replacement safe: the order id is
        preserved and there is never a window with two stops or none.
        """
