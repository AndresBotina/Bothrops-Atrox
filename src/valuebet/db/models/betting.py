"""Modelos del esquema `betting`: señales de valor, paper bets y banca."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from valuebet.db.models.base import Base


class ValueSignal(Base):
    __tablename__ = "value_signals"
    __table_args__ = (
        Index("idx_value_signals_pred", "prediction_id"),
        {"schema": "betting"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.predictions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bookmaker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market.bookmakers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    odds_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market.odds_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_prob: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    market_implied_prob: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    edge: Mapped[Decimal] = mapped_column(Numeric(7, 5), nullable=False)
    expected_value: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PaperBet(Base):
    __tablename__ = "paper_bets"
    __table_args__ = (
        CheckConstraint("stake > 0", name="stake_positive"),
        CheckConstraint(
            "status IN ('open', 'won', 'lost', 'void', 'pushed', 'half_won', 'half_lost')",
            name="status",
        ),
        Index("idx_paper_bets_status", "status"),
        {"schema": "betting"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    value_signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("betting.value_signals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    kelly_fraction: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    decimal_odds_taken: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    closing_decimal_odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BankrollLedger(Base):
    __tablename__ = "bankroll_ledger"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('deposit', 'withdrawal', 'bet_stake', 'bet_return', 'adjustment')",
            name="entry_type",
        ),
        Index("idx_ledger_time", "occurred_at"),
        {"schema": "betting"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paper_bet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("betting.paper_bets.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
