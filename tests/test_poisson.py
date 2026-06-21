"""HU 3.1 — Modelo Poisson: recuperación de parámetros, validez y dirección.

Datos SINTÉTICOS generados con parámetros conocidos (sin API real). Requiere el
extra 'modeling' (numpy/scipy); si no está, los tests se omiten.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("scipy")
import numpy as np  # noqa: E402

from valuebet.evaluation.backtest import walk_forward  # noqa: E402
from valuebet.evaluation.baselines import build_model  # noqa: E402
from valuebet.evaluation.model import Match, PredictionModel  # noqa: E402
from valuebet.modeling.poisson import PoissonModel  # noqa: E402

_T0 = datetime(2023, 8, 1, 14, 0, tzinfo=UTC)


def _match(home, away, hg, ag, *, n=0) -> Match:
    return Match(uuid.uuid4(), _T0 + timedelta(hours=n), home, away, int(hg), int(ag))


def _synthetic_league(
    rng, *, n_teams: int, n_matches: int, attack: dict, defense: dict, home_adv: float
) -> list[Match]:
    teams = list(attack)
    matches: list[Match] = []
    for k in range(n_matches):
        i, j = rng.choice(n_teams, size=2, replace=False)
        th, ta = teams[i], teams[j]
        lam_home = np.exp(attack[th] + defense[ta] + home_adv)
        lam_away = np.exp(attack[ta] + defense[th])
        matches.append(_match(th, ta, rng.poisson(lam_home), rng.poisson(lam_away), n=k))
    return matches


def test_poisson_satisfies_protocol() -> None:
    assert isinstance(PoissonModel(), PredictionModel)


# --- (a) recuperación de parámetros conocidos --------------------------------
def test_fit_recovers_known_parameters() -> None:
    rng = np.random.default_rng(42)
    teams = [uuid.uuid4() for _ in range(12)]
    # Ataques simétricos → media 0 (parametrización canónica del modelo).
    attack = dict(zip(teams, np.linspace(-0.6, 0.6, 12), strict=True))
    defense = dict(zip(teams, np.linspace(-0.4, 0.4, 12), strict=True))
    home_adv = 0.30

    matches = _synthetic_league(
        rng, n_teams=12, n_matches=9000, attack=attack, defense=defense, home_adv=home_adv
    )
    model = PoissonModel()
    model.fit(matches)
    params = model.parameters

    for t in teams:
        assert params.attack[t] == pytest.approx(attack[t], abs=0.12)
        assert params.defense[t] == pytest.approx(defense[t], abs=0.12)
    assert params.home_advantage == pytest.approx(home_adv, abs=0.08)
    # El re-centrado deja la media de ataques en 0.
    assert np.mean(list(params.attack.values())) == pytest.approx(0.0, abs=1e-9)


# --- (b) probabilidades válidas ----------------------------------------------
def test_predict_proba_is_valid_distribution() -> None:
    rng = np.random.default_rng(1)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _synthetic_league(
        rng, n_teams=8, n_matches=2000, attack=attack, defense=defense, home_adv=0.25
    )
    model = PoissonModel()
    model.fit(matches)

    for _ in range(20):
        i, j = rng.choice(8, size=2, replace=False)
        probs = model.predict_proba(_match(teams[i], teams[j], 0, 0))
        for p in (probs.home, probs.draw, probs.away):
            assert 0.0 <= p <= 1.0
        assert probs.home + probs.draw + probs.away == pytest.approx(1.0, abs=1e-9)


# --- (c) dirección: fuerte vence a débil -------------------------------------
def test_stronger_team_more_likely_to_win() -> None:
    rng = np.random.default_rng(7)
    strong, weak = uuid.uuid4(), uuid.uuid4()
    attack = {strong: 0.7, weak: -0.7}
    defense = {strong: -0.4, weak: 0.4}
    matches = _synthetic_league(
        rng, n_teams=2, n_matches=1500, attack=attack, defense=defense, home_adv=0.25
    )
    model = PoissonModel()
    model.fit(matches)

    # Fuerte de local contra débil: gana el local con holgura.
    p1 = model.predict_proba(_match(strong, weak, 0, 0))
    assert p1.home > p1.away
    # Débil de local contra fuerte: gana el visitante pese a la ventaja de campo.
    p2 = model.predict_proba(_match(weak, strong, 0, 0))
    assert p2.away > p2.home


# --- (d) equipo sin historia: fallback neutro, no rompe ----------------------
def test_unknown_team_uses_neutral_fallback() -> None:
    rng = np.random.default_rng(3)
    teams = [uuid.uuid4() for _ in range(6)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 6), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 6), strict=True))
    matches = _synthetic_league(
        rng, n_teams=6, n_matches=1200, attack=attack, defense=defense, home_adv=0.25
    )
    model = PoissonModel()
    model.fit(matches)

    newcomer = uuid.uuid4()  # nunca visto en el entrenamiento
    probs = model.predict_proba(_match(newcomer, teams[0], 0, 0))
    assert probs.home + probs.draw + probs.away == pytest.approx(1.0, abs=1e-9)
    # También como visitante.
    probs2 = model.predict_proba(_match(teams[0], newcomer, 0, 0))
    assert probs2.home + probs2.draw + probs2.away == pytest.approx(1.0, abs=1e-9)


def test_predict_before_fit_returns_uniform() -> None:
    model = PoissonModel()
    probs = model.predict_proba(_match(uuid.uuid4(), uuid.uuid4(), 0, 0))
    assert probs.home == pytest.approx(1 / 3)


# --- (e) integración con el walk-forward de la Fase 2 ------------------------
def test_integrates_with_walk_forward() -> None:
    rng = np.random.default_rng(11)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _synthetic_league(
        rng, n_teams=8, n_matches=400, attack=attack, defense=defense, home_adv=0.25
    )
    result = walk_forward(PoissonModel(), matches, step=5, min_train=40)
    assert result.n_predicted > 0
    for r in result.records:
        assert r.probs.home + r.probs.draw + r.probs.away == pytest.approx(1.0, abs=1e-9)


def test_build_model_registers_poisson() -> None:
    assert isinstance(build_model("poisson"), PoissonModel)
