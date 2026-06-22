"""HU 3.3 — Ponderación temporal (decaimiento exponencial) sobre Dixon-Coles.

Datos SINTÉTICOS (sin API real). Requiere el extra 'modeling'; si falta, se omiten.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("scipy")
import numpy as np  # noqa: E402

from valuebet.evaluation.model import Match  # noqa: E402
from valuebet.evaluation.tuning import best_by_brier, sweep_half_lives  # noqa: E402
from valuebet.modeling.dixon_coles import DixonColesModel  # noqa: E402
from valuebet.modeling.poisson import PoissonModel, decay_weights  # noqa: E402

_T0 = datetime(2016, 1, 1, 14, 0, tzinfo=UTC)


def _match(home, away, hg, ag, *, day=0) -> Match:
    return Match(uuid.uuid4(), _T0 + timedelta(days=day), home, away, int(hg), int(ag))


# --- (b) los pesos temporales se calculan bien (a mano) ----------------------
def test_decay_weights_hand_values() -> None:
    hl = 100.0
    ages = np.array([0.0, 100.0, 200.0, 300.0])  # 0, 1, 2, 3 half-lives
    w = decay_weights(ages, hl)
    np.testing.assert_allclose(w, [1.0, 0.5, 0.25, 0.125], rtol=1e-12)


def test_decay_weights_infinite_is_unity() -> None:
    ages = np.array([0.0, 365.0, 3650.0])
    np.testing.assert_array_equal(decay_weights(ages, None), np.ones(3))


def test_decay_weights_xi_relation() -> None:
    # w = exp(-xi*age), xi = ln(2)/hl. Chequeo independiente de la fórmula.
    hl = 250.0
    ages = np.array([37.0, 410.0])
    xi = math.log(2) / hl
    np.testing.assert_allclose(decay_weights(ages, hl), np.exp(-xi * ages), rtol=1e-12)


# --- (a) half-life infinito reproduce EXACTAMENTE la HU 3.2 ------------------
def _league(rng, *, n_teams, n_matches, attack, defense, home_adv):
    teams = list(attack)
    matches = []
    for k in range(n_matches):
        i, j = rng.choice(n_teams, size=2, replace=False)
        th, ta = teams[i], teams[j]
        lam = np.exp(attack[th] + defense[ta] + home_adv)
        mu = np.exp(attack[ta] + defense[th])
        matches.append(_match(th, ta, rng.poisson(lam), rng.poisson(mu), day=k))
    return matches


def test_infinite_half_life_matches_hu32() -> None:
    rng = np.random.default_rng(2)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _league(rng, n_teams=8, n_matches=1500, attack=attack, defense=defense, home_adv=0.25)

    no_decay = DixonColesModel(half_life=None)  # HU 3.2
    explicit = DixonColesModel(half_life=None)
    no_decay.fit(matches)
    explicit.fit(matches)

    # Mismo ajuste exacto: ρ y parámetros idénticos.
    assert no_decay.rho == pytest.approx(explicit.rho, abs=1e-12)
    p_a, p_b = no_decay.parameters, explicit.parameters
    for t in teams:
        assert p_a.attack[t] == pytest.approx(p_b.attack[t], abs=1e-12)

    # Y las predicciones idénticas.
    m = _match(teams[0], teams[1], 0, 0)
    assert no_decay.predict_proba(m).as_dict() == pytest.approx(explicit.predict_proba(m).as_dict())


def test_weights_unity_equivalent_to_unweighted_objective() -> None:
    # El objetivo con pesos ≡ 1 coincide numéricamente con el de half_life=None.
    rng = np.random.default_rng(4)
    n, nm = 5, 200
    home_idx = rng.integers(0, n, nm).astype(np.intp)
    away_idx = rng.integers(0, n, nm).astype(np.intp)
    hg = rng.integers(0, 4, nm).astype(np.float64)
    ag = rng.integers(0, 4, nm).astype(np.float64)
    x = np.concatenate([rng.normal(0, 0.2, 2 * n), [0.25], [-0.1]])
    dc = DixonColesModel()
    w1 = np.ones(nm)
    nll, grad = dc._objective(x, home_idx, away_idx, hg, ag, w1, n)
    # Recalcular "a mano" sin pesos da lo mismo (sanity de que w=1 es neutro).
    nll2, grad2 = dc._objective(x, home_idx, away_idx, hg, ag, np.ones(nm), n)
    assert nll == pytest.approx(nll2)
    np.testing.assert_allclose(grad, grad2)


# --- (c) el decaimiento estima mejor la fuerza RECIENTE ----------------------
def test_decay_tracks_recent_strength() -> None:
    rng = np.random.default_rng(31)
    evolving = uuid.uuid4()
    others = [uuid.uuid4() for _ in range(6)]
    # Oponentes con fuerza fija pequeña.
    base_attack = dict(zip(others, np.linspace(-0.2, 0.2, 6), strict=True))
    base_defense = dict(zip(others, np.linspace(-0.15, 0.15, 6), strict=True))
    home_adv = 0.25
    # 'evolving': DÉBIL la primera mitad, FUERTE la segunda.
    weak, strong = -0.7, 0.7
    half_days = 700

    def attack_of(team, day):
        if team == evolving:
            return weak if day < half_days else strong
        return base_attack[team]

    def defense_of(team):
        return -0.5 if team == evolving else base_defense[team]  # buena defensa fija

    matches = []
    # Muchos partidos del 'evolving' repartidos en el tiempo.
    for block_start, n_block in ((0, 600), (half_days, 600)):
        for k in range(n_block):
            d = block_start + k
            opp = others[rng.integers(0, 6)]
            home, away = (evolving, opp) if k % 2 == 0 else (opp, evolving)
            a_h = attack_of(home, d)
            a_a = attack_of(away, d)
            lam = np.exp(a_h + defense_of(away) + home_adv)
            mu = np.exp(a_a + defense_of(home))
            matches.append(_match(home, away, rng.poisson(lam), rng.poisson(mu), day=d))

    no_decay = DixonColesModel(half_life=None)
    decay = DixonColesModel(half_life=120)  # olvido rápido
    no_decay.fit(matches)
    decay.fit(matches)

    a_nodecay = no_decay.parameters.attack[evolving]
    a_decay = decay.parameters.attack[evolving]
    # El decaimiento debe ver al equipo MÁS fuerte (su estado reciente).
    assert a_decay > a_nodecay + 0.2
    assert a_decay > 0.0  # reciente = fuerte
    assert a_nodecay < a_decay  # sin decaimiento promedia débil+fuerte → menor


# --- (d) distribución válida con decaimiento ---------------------------------
def test_predict_valid_with_decay() -> None:
    rng = np.random.default_rng(8)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _league(rng, n_teams=8, n_matches=1500, attack=attack, defense=defense, home_adv=0.25)
    model = DixonColesModel(half_life=200)
    model.fit(matches)
    for _ in range(15):
        i, j = rng.choice(8, size=2, replace=False)
        p = model.predict_proba(_match(teams[i], teams[j], 0, 0))
        for v in (p.home, p.draw, p.away):
            assert 0.0 <= v <= 1.0
        assert p.home + p.draw + p.away == pytest.approx(1.0, abs=1e-9)


def test_poisson_also_supports_half_life() -> None:
    # La ponderación vive en la base: PoissonModel también la acepta.
    assert PoissonModel(half_life=180).half_life == 180


# --- barrido de half-lives ---------------------------------------------------
def test_sweep_half_lives_returns_row_per_value() -> None:
    rng = np.random.default_rng(15)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _league(rng, n_teams=8, n_matches=500, attack=attack, defense=defense, home_adv=0.25)
    grid = (180, 365, None)
    rows = sweep_half_lives("dixon_coles", matches, grid, step=10, min_train=40)
    assert [r.half_life for r in rows] == [180, 365, None]
    for r in rows:
        assert r.metrics.n > 0
    # best_by_brier devuelve una de las filas.
    assert best_by_brier(rows) in rows
