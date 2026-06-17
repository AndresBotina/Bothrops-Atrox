"""Modelos del esquema `models`: registro de modelos y predicciones."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from valuebet.db.models.base import Base


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        {"schema": "models"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    hyperparameters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    feature_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("features.feature_sets.id", ondelete="RESTRICT")
    )
    train_data_from: Mapped[date | None] = mapped_column(Date)
    train_data_to: Mapped[date | None] = mapped_column(Date)
    code_hash: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint("predicted_prob BETWEEN 0 AND 1", name="predicted_prob_range"),
        UniqueConstraint("match_id", "model_version_id", "market_type", "selection_code", "line"),
        Index("idx_predictions_match", "match_id", "model_version_id"),
        {"schema": "models"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core.matches.id", ondelete="RESTRICT"), nullable=False
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.model_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(
        Text, ForeignKey("market.market_types.code", ondelete="RESTRICT"), nullable=False
    )
    selection_code: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    predicted_prob: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    feature_row_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("features.match_team_features.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
