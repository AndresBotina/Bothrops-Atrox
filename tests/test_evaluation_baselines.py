"""HU 2.1 — Baselines: frecuencias correctas y contrato PredictionModel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from valuebet.evaluation.baselines import (
    HomeAdvantageBaseline,
    TeamFrequencyBaseline,
    build_model,
)
from valuebet.evaluation.model import Match, PredictionModel

_T0 = datetime(2023, 8, 1, 14, 0, tzinfo=UTC)
_HOME = uuid.uuid4()
_AWAY = uuid.uuid4()


def _match(hg: int, ag: int, *, n: int = 0, home=_HOME, away=_AWAY) -> Match:
    return Match(
        match_id=uuid.uuid4(),
        kickoff_utc=_T0 + timedelta(days=n),
        home_team_id=home,
        away_team_id=away,
        home_goals=hg,
        away_goals=ag,
    )


def test_baselines_satisfy_protocol() -> None:
    assert isinstance(HomeAdvantageBaseline(), PredictionModel)
    assert isinstance(TeamFrequencyBaseline(), PredictionModel)


def test_home_advantage_uses_global_frequencies() -> None:
    # 5 locales, 3 empates, 2 visitantes.
    matches = (
        [_match(2, 0, n=i) for i in range(5)]
        + [_match(1, 1, n=5 + i) for i in range(3)]
        + [_match(0, 1, n=8 + i) for i in range(2)]
    )
    model = HomeAdvantageBaseline()
    model.fit(matches)
    probs = model.predict_proba(_match(0, 0))
    assert probs.home == pytest.approx(0.5)
    assert probs.draw == pytest.approx(0.3)
    assert probs.away == pytest.approx(0.2)


def test_home_advantage_is_constant_across_matches() -> None:
    model = HomeAdvantageBaseline()
    model.fit([_match(1, 0), _match(0, 1)])
    other_home, other_away = uuid.uuid4(), uuid.uuid4()
    p1 = model.predict_proba(_match(0, 0))
    p2 = model.predict_proba(_match(0, 0, home=other_home, away=other_away))
    assert p1.as_dict() == p2.as_dict()


def test_empty_fit_falls_back_to_uniform() -> None:
    model = HomeAdvantageBaseline()
    model.fit([])
    probs = model.predict_proba(_match(0, 0))
    assert probs.home == pytest.approx(1 / 3)
    assert probs.draw == pytest.approx(1 / 3)
    assert probs.away == pytest.approx(1 / 3)


def test_team_frequency_reacts_to_strong_home_team() -> None:
    strong = uuid.uuid4()
    weak = uuid.uuid4()
    # 'strong' gana TODOS sus partidos en casa contra 'weak'.
    matches = [_match(3, 0, n=i, home=strong, away=weak) for i in range(10)]
    model = TeamFrequencyBaseline(smoothing=1.0)
    model.fit(matches)
    probs = model.predict_proba(_match(0, 0, home=strong, away=weak))
    # La probabilidad de local debe dominar claramente al empate y la visita.
    assert probs.home > probs.draw
    assert probs.home > probs.away
    assert probs.home + probs.draw + probs.away == pytest.approx(1.0, abs=1e-9)


def test_team_frequency_unknown_team_uses_global() -> None:
    matches = [_match(1, 0, n=i) for i in range(4)] + [_match(0, 1, n=4 + i) for i in range(4)]
    model = TeamFrequencyBaseline(smoothing=2.0)
    model.fit(matches)
    probs = model.predict_proba(_match(0, 0, home=uuid.uuid4(), away=uuid.uuid4()))
    # Equipos nunca vistos → distribución equivale a la global (sin sesgo extra).
    assert probs.home + probs.draw + probs.away == pytest.approx(1.0, abs=1e-9)
    assert probs.home == pytest.approx(0.5)
    assert probs.away == pytest.approx(0.5)
    assert probs.draw == pytest.approx(0.0, abs=1e-9)


def test_build_model_registry() -> None:
    assert isinstance(build_model("baseline"), HomeAdvantageBaseline)
    assert isinstance(build_model("home-advantage"), HomeAdvantageBaseline)
    assert isinstance(build_model("team-frequency"), TeamFrequencyBaseline)
    with pytest.raises(KeyError):
        build_model("dixon-coles")
