"""Order domain models: intents, orders, sides and statuses."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from polybot.utils.time_utils import now_s

_seq = itertools.count(1)


class Action(str, Enum):
    """Buy or sell, in exchange terms."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Local lifecycle of an order, mirroring CLOB states."""

    PENDING = "pending"            # created locally, not yet on the book
    LIVE = "live"                  # resting on the book
    MATCHED = "matched"            # matched, settlement pending on-chain
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"          # exchange rejected the submission
    FAILED = "failed"             # local/transport failure submitting

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        }

    @property
    def is_open(self) -> bool:
        return self in {OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED, OrderStatus.MATCHED}


@dataclass(frozen=True)
class OrderIntent:
    """A strategy-originated instruction to place a single order."""

    market_id: str
    strategy_side: str      # 'up' | 'down' (the outcome token)
    token_id: str
    action: Action          # BUY | SELL
    price: float
    size: float
    order_type: str = "GTC"  # GTC | GTD | FOK | FAK
    created_ts: float = field(default_factory=now_s)

    @property
    def notional(self) -> float:
        return self.price * self.size

    def dedup_key(self) -> tuple[str, str, str, float, float]:
        """Identity used to suppress duplicate orders within a short window."""
        return (self.token_id, self.action.value, self.order_type, round(self.price, 4), round(self.size, 2))


@dataclass
class Order:
    """A tracked order with live fill/status state."""

    intent: OrderIntent
    client_id: str = field(default_factory=lambda: f"c{next(_seq):08d}")
    exchange_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    created_ts: float = field(default_factory=now_s)
    updated_ts: float = field(default_factory=now_s)
    submitted_ts: float | None = None   # when the exchange accepted the submission
    fees: float = 0.0                   # cumulative fees charged across fills
    last_error: str | None = None
    simulated: bool = False  # True when placed in dry-run mode (never submitted)

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.intent.size - self.filled_size)

    @property
    def open_notional(self) -> float:
        """Notional still working (unfilled) on the book, 0 once terminal."""
        if self.status.is_terminal:
            return 0.0
        return self.intent.price * self.remaining_size

    def touch(self) -> None:
        self.updated_ts = now_s()
