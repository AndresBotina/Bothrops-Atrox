"""Modelos del esquema `features`: feature store point-in-time."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from valuebet.db.models.base import Base


class FeatureSet(Base):
    __tablename__ = "feature_sets"
    __table_args__ = ({"schema": "features"},)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MatchTeamFeatures(Base):
    __tablename__ = "match_team_features"
    __table_args__ = (
        UniqueConstraint("match_id", "team_id", "feature_set_id", "as_of_timestamp"),
        Index("idx_features_match", "match_id", "feature_set_id"),
        {"schema": "features"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core.matches.id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core.teams.id", ondelete="RESTRICT"), nullable=False
    )
    feature_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("features.feature_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    as_of_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
