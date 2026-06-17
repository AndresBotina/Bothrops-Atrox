"""Modelos del esquema `raw`: zona de aterrizaje inmutable (append-only)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from valuebet.db.models.base import Base


class Payload(Base):
    __tablename__ = "payloads"
    __table_args__ = (
        Index("idx_raw_payloads_source", "source_id", text("fetched_at DESC")),
        Index("idx_raw_payloads_hash", "request_hash"),
        {"schema": "raw"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meta.sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meta.ingestion_runs.id", ondelete="RESTRICT")
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
