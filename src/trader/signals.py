"""Signal: the one message format every trade idea uses.

Strategies, the webhook and (later) manual tools all produce a `Signal`; the
risk engine is the only consumer allowed to turn it into an order (rule 2).

A signal is deliberately *permissive* about things the risk engine polices
(e.g. a missing stop-loss). If this class refused them, a bad webhook payload
would die silently here instead of being rejected and logged with a reason.
It only guards basic well-formedness.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

SIDES = ("BUY", "SELL")
OPT_TYPES = ("CE", "PE")
KINDS = ("ENTRY", "EXIT")


@dataclass(frozen=True)
class Leg:
    underlying: str
    expiry: date
    strike: float
    opt_type: str  # "CE" | "PE"
    side: str  # "BUY" | "SELL"
    lots: int
    lot_size: int
    symbol: str  # e.g. NIFTY17NOV2625000CE
    token: str  # NFO instrument token (resolved, so the risk engine can verify it)

    def __post_init__(self) -> None:
        if self.opt_type not in OPT_TYPES:
            raise ValueError(f"opt_type must be one of {OPT_TYPES}, got {self.opt_type!r}")
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {self.side!r}")
        if not isinstance(self.lots, int) or self.lots < 1:
            raise ValueError(f"lots must be a positive integer, got {self.lots!r}")
        if not isinstance(self.lot_size, int) or self.lot_size < 1:
            raise ValueError(f"lot_size must be a positive integer, got {self.lot_size!r}")
        if self.strike <= 0:
            raise ValueError("strike must be positive")

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    def opposite(self) -> "Leg":
        """The same contract on the other side — used to close a position."""
        return replace(self, side="SELL" if self.side == "BUY" else "BUY")


@dataclass(frozen=True)
class Signal:
    idempotency_key: str  # same key => same intent; the risk engine drops repeats
    strategy: str
    kind: str  # "ENTRY" | "EXIT"
    legs: tuple[Leg, ...]
    reason: str  # human-readable "why", stored in the journal
    created_at: datetime  # timezone-aware
    # Premium levels per unit for the whole structure (single long option: the
    # option's price). Optional here on purpose — see module docstring.
    stop_loss: float | None = None
    target: float | None = None
    ref_price: float | None = None  # price assumed when the idea was formed (for slippage stats)
    position_id: str | None = None  # EXIT: which position to close
    source: str = "strategy"  # "strategy" | "webhook"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        object.__setattr__(self, "legs", tuple(self.legs))
        if not self.legs:
            raise ValueError("a signal needs at least one leg")
        if not self.idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True)
class PositionView:
    """Read-only snapshot of an open position (used by strategies and the risk engine)."""
    position_id: str
    strategy: str
    legs: tuple[Leg, ...]
    entry_price: float  # net premium per unit at entry (debit > 0, credit < 0)
    stop_loss: float | None
    target: float | None
    opened_at: datetime
