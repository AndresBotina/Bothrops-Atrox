"""Esquemas Pydantic para validar el CATÁLOGO crudo de API-Football.

Validan SÓLO los campos que vamos a usar y toleran campos extra. Modelan como
opcionales los que la API a veces devuelve `null` (founded, capacity, code, …).
NO son las tablas `core`: la normalización es trabajo de la HU 1.3.2.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    # Ignora campos que no modelamos (la API devuelve muchos más).
    model_config = ConfigDict(extra="ignore")


# ---- /leagues ---------------------------------------------------------------
class Country(_Base):
    name: str
    code: str | None = None


class Season(_Base):
    year: int
    start: date | None = None
    end: date | None = None
    current: bool | None = None


class League(_Base):
    id: int
    name: str
    type: str | None = None


class LeagueEntry(_Base):
    league: League
    country: Country
    seasons: list[Season] = []


# ---- /teams (venue embebido) ------------------------------------------------
class Team(_Base):
    id: int
    name: str
    code: str | None = None
    founded: int | None = None
    national: bool | None = None


class Venue(_Base):
    id: int | None = None
    name: str | None = None
    city: str | None = None
    capacity: int | None = None


class TeamEntry(_Base):
    team: Team
    venue: Venue


def parse_leagues(payload: dict) -> list[LeagueEntry]:
    """Valida y devuelve las entradas de liga de un payload `/leagues`."""
    return [LeagueEntry.model_validate(item) for item in payload.get("response", [])]


def parse_teams(payload: dict) -> list[TeamEntry]:
    """Valida y devuelve las entradas de equipo de un payload `/teams`."""
    return [TeamEntry.model_validate(item) for item in payload.get("response", [])]


# ---- /fixtures --------------------------------------------------------------
class FixtureStatus(_Base):
    short: str
    long: str | None = None


class FixtureVenueRef(_Base):
    id: int | None = None


class FixtureInfo(_Base):
    id: int
    date: datetime | None = None  # ISO con tz
    timestamp: int | None = None  # epoch UTC
    timezone: str | None = None
    status: FixtureStatus
    venue: FixtureVenueRef = Field(default_factory=FixtureVenueRef)


class FixtureLeagueRef(_Base):
    id: int
    season: int
    round: str | None = None


class TeamRef(_Base):
    id: int


class FixtureTeams(_Base):
    home: TeamRef
    away: TeamRef


class Goals(_Base):
    # null cuando el partido no se ha jugado: opcionales reales.
    home: int | None = None
    away: int | None = None


class ScoreHalf(_Base):
    home: int | None = None
    away: int | None = None


class Score(_Base):
    halftime: ScoreHalf = Field(default_factory=ScoreHalf)


class FixtureEntry(_Base):
    fixture: FixtureInfo
    league: FixtureLeagueRef
    teams: FixtureTeams
    goals: Goals = Field(default_factory=Goals)
    score: Score = Field(default_factory=Score)


def parse_fixtures(payload: dict) -> list[FixtureEntry]:
    """Valida y devuelve las entradas de partido de un payload `/fixtures`."""
    return [FixtureEntry.model_validate(item) for item in payload.get("response", [])]


# ---- /fixtures/statistics ---------------------------------------------------
class StatItem(_Base):
    type: str
    # value puede ser null, entero, o texto ("55%", "1.8"): no asumimos numérico.
    value: Any = None


class TeamStatistics(_Base):
    team: TeamRef
    statistics: list[StatItem] = []


def parse_statistics(payload: dict) -> list[TeamStatistics]:
    """Valida y devuelve las estadísticas por equipo de un payload `/fixtures/statistics`."""
    return [TeamStatistics.model_validate(item) for item in payload.get("response", [])]
