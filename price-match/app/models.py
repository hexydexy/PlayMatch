from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Index, Integer,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    norm_title: Mapped[str] = mapped_column(String(300), index=True)
    release_year: Mapped[int | None] = mapped_column(Integer)
    steam_app_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    listings: Mapped[list["Listing"]] = relationship(back_populates="game")


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # steam, gog, epic, g2a
    name: Mapped[str] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(20))  # first_party | marketplace


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("store_id", "store_product_id", "region"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"))
    store_product_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(600))
    region: Mapped[str] = mapped_column(String(8), default="US")
    game: Mapped[Game] = relationship(back_populates="listings")
    store: Mapped[Store] = relationship()


class PriceSnapshot(Base):
    """Append-only price history."""
    __tablename__ = "price_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    price_cents: Mapped[int] = mapped_column(Integer)
    base_price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    listing: Mapped[Listing] = relationship()

    __table_args__ = (Index("ix_snap_listing_time", "listing_id", "captured_at"),)


class MatchCandidate(Base):
    """Uncertain matches awaiting manual review."""
    __tablename__ = "match_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    store_id: Mapped[str] = mapped_column(String(20))
    store_product_id: Mapped[str] = mapped_column(String(200))
    store_title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(String(600))
    score: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|approved|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[float] = mapped_column(Float)
    fetched_on: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    __table_args__ = (UniqueConstraint("base", "quote", "fetched_on"),)


class JobRun(Base):
    """One row per (job, store, region) outcome. Feeds reliability metrics."""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # ingest | gogdb_import
    store_id: Mapped[str] = mapped_column(String(20))
    region: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(16))  # ok | no_match | error | rate_limited
    detail: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PriceCheck(Base):
    """When a store was last asked about a game.

    A snapshot's timestamp is only the last price *change* (unchanged prices are
    not re-stored), so "last checked" needs its own record. It also orders the bulk
    Steam refresh (never-checked games first) and drives the Epic on-demand cooldown.
    """
    __tablename__ = "price_checks"
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    region: Mapped[str] = mapped_column(String(8), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
