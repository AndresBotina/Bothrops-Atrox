"""Modelos del esquema `market`: cuotas y mercados (bitemporal)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from valuebet.db.models.base import Base


class Bookmaker(Base):
    __tablename__ = "bookmakers"
    __table_args__ = ({"schema": "market"},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_sharp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    country: Mapped[str | None] = mapped_column(Text)


class MarketType(Base):
    __tablename__ = "market_types"
    __table_args__ = ({"schema": "market"},)

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    has_line: Mapped[bool] = mapped_column(Boolean, nullable=False)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        CheckConstraint("decimal_odds > 1.0", name="decimal_odds_gt_one"),
        UniqueConstraint(
            "match_id", "bookmaker_id", "market_type", "selection_code", "line", "recorded_at"
        ),
        Index("idx_odds_match_time", "match_id", text("recorded_at DESC")),
        Index("idx_odds_match_mkt", "match_id", "market_type", "selection_code"),
        {"schema": "market"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core.matches.id", ondelete="RESTRICT"), nullable=False
    )
    bookmaker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market.bookmakers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(
        Text, ForeignKey("market.market_types.code", ondelete="RESTRICT"), nullable=False
    )
    selection_code: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    decimal_odds: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw.payloads.id", ondelete="RESTRICT")
    )
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meta.ingestion_runs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
