"""Esquemas Pydantic para validar el CATÁLOGO crudo de API-Football.

Validan SÓLO los campos que vamos a usar y toleran campos extra. Modelan como
opcionales los que la API a veces devuelve `null` (founded, capacity, code, …).
NO son las tablas `core`: la normalización es trabajo de la HU 1.3.2.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


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
