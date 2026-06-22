"""HU 3.2 — Dixon-Coles: recuperación de ρ, dirección del efecto y consistencia.

Datos SINTÉTICOS con dependencia ρ conocida (sin API real). Requiere el extra
'modeling' (numpy/scipy); si falta, se omiten.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("scipy")
import numpy as np  # noqa: E402
from scipy.optimize import check_grad  # noqa: E402

from valuebet.evaluation.backtest import walk_forward  # noqa: E402
from valuebet.evaluation.baselines import build_model  # noqa: E402
from valuebet.evaluation.model import Match, PredictionModel  # noqa: E402
from valuebet.modeling.dixon_coles import DixonColesModel  # noqa: E402
from valuebet.modeling.poisson import PoissonModel  # noqa: E402

_T0 = datetime(2023, 8, 1, 14, 0, tzinfo=UTC)


def _match(home, away, hg, ag, *, n=0) -> Match:
    return Match(uuid.uuid4(), _T0 + timedelta(hours=n), home, away, int(hg), int(ag))


def _dc_score_matrix(lam: float, mu: float, rho: float, max_goals: int = 10) -> np.ndarray:
    ks = np.arange(max_goals + 1)
    from scipy.special import gammaln

    ph = np.exp(-lam + ks * np.log(lam) - gammaln(ks + 1))
    pa = np.exp(-mu + ks * np.log(mu) - gammaln(ks + 1))
    joint = np.outer(ph, pa)
    joint[0, 0] *= 1.0 - lam * mu * rho
    joint[0, 1] *= 1.0 + lam * rho
    joint[1, 0] *= 1.0 + mu * rho
    joint[1, 1] *= 1.0 - rho
    return np.clip(joint, 0.0, None)


def _synthetic_dc_league(rng, *, n_teams, n_matches, attack, defense, home_adv, rho):
    """Genera partidos muestreando del modelo Dixon-Coles con ρ conocido."""
    teams = list(attack)
    max_goals = 10
    flat_idx = np.arange((max_goals + 1) ** 2)
    matches: list[Match] = []
    for k in range(n_matches):
        i, j = rng.choice(n_teams, size=2, replace=False)
        th, ta = teams[i], teams[j]
        lam = np.exp(attack[th] + defense[ta] + home_adv)
        mu = np.exp(attack[ta] + defense[th])
        probs = _dc_score_matrix(lam, mu, rho, max_goals).ravel()
        probs /= probs.sum()
        cell = rng.choice(flat_idx, p=probs)
        hg, ag = divmod(int(cell), max_goals + 1)
        matches.append(_match(th, ta, hg, ag, n=k))
    return matches


def test_dc_satisfies_protocol() -> None:
    assert isinstance(DixonColesModel(), PredictionModel)


# --- gradiente analítico correcto (cimiento del MLE) -------------------------
def test_analytic_gradient_matches_numeric() -> None:
    rng = np.random.default_rng(0)
    n = 5
    n_matches = 300
    home_idx = rng.integers(0, n, n_matches).astype(np.intp)
    away_idx = rng.integers(0, n, n_matches).astype(np.intp)
    hg = rng.integers(0, 4, n_matches).astype(np.float64)
    ag = rng.integers(0, 4, n_matches).astype(np.float64)
    model = DixonColesModel()

    x = np.concatenate([rng.normal(0, 0.2, 2 * n), [0.25], [-0.12]])  # a,d,h,rho
    weights = rng.uniform(0.3, 1.0, n_matches)  # pesos no triviales (gradiente ponderado)

    def f(z):
        return model._objective(z, home_idx, away_idx, hg, ag, weights, n)[0]

    def g(z):
        return model._objective(z, home_idx, away_idx, hg, ag, weights, n)[1]

    assert check_grad(f, g, x) < 1e-4


# --- (a) recuperación de ρ (y demás parámetros) ------------------------------
def test_fit_recovers_known_rho() -> None:
    rng = np.random.default_rng(123)
    teams = [uuid.uuid4() for _ in range(12)]
    attack = dict(zip(teams, np.linspace(-0.6, 0.6, 12), strict=True))
    defense = dict(zip(teams, np.linspace(-0.4, 0.4, 12), strict=True))
    home_adv = 0.30
    true_rho = -0.13

    matches = _synthetic_dc_league(
        rng,
        n_teams=12,
        n_matches=16000,
        attack=attack,
        defense=defense,
        home_adv=home_adv,
        rho=true_rho,
    )
    model = DixonColesModel()
    model.fit(matches)
    params = model.parameters

    assert params.rho == pytest.approx(true_rho, abs=0.05)
    assert params.home_advantage == pytest.approx(home_adv, abs=0.08)
    for t in teams:
        assert params.attack[t] == pytest.approx(attack[t], abs=0.12)
        assert params.defense[t] == pytest.approx(defense[t], abs=0.12)


# --- (b) la corrección sube los marcadores bajos con ρ<0 ---------------------
def test_correction_raises_low_scores_when_rho_negative() -> None:
    poisson = PoissonModel(max_goals=10)
    dc = DixonColesModel(max_goals=10)
    dc._rho = -0.10  # ρ típico de fútbol

    lam, mu = 1.5, 1.2
    poisson_joint = poisson._joint_matrix(lam, mu)
    dc_joint = dc._joint_matrix(lam, mu)

    # 0-0 y 1-1 AUMENTAN; 0-1 y 1-0 disminuyen (signo de τ con ρ<0).
    assert dc_joint[0, 0] > poisson_joint[0, 0]
    assert dc_joint[1, 1] > poisson_joint[1, 1]
    assert dc_joint[0, 1] < poisson_joint[0, 1]
    assert dc_joint[1, 0] < poisson_joint[1, 0]
    # Celda alta intacta (τ = 1 para x>=2 o y>=2).
    assert dc_joint[2, 3] == pytest.approx(poisson_joint[2, 3])


# --- (c) distribución válida tras la corrección ------------------------------
def test_predict_proba_valid_after_correction() -> None:
    rng = np.random.default_rng(5)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _synthetic_dc_league(
        rng, n_teams=8, n_matches=2500, attack=attack, defense=defense, home_adv=0.25, rho=-0.12
    )
    model = DixonColesModel()
    model.fit(matches)
    for _ in range(20):
        i, j = rng.choice(8, size=2, replace=False)
        probs = model.predict_proba(_match(teams[i], teams[j], 0, 0))
        for p in (probs.home, probs.draw, probs.away):
            assert 0.0 <= p <= 1.0
        assert probs.home + probs.draw + probs.away == pytest.approx(1.0, abs=1e-9)


# --- (d) ρ = 0 reproduce EXACTAMENTE el Poisson ------------------------------
def test_rho_zero_matches_poisson_matrix() -> None:
    poisson = PoissonModel(max_goals=10)
    dc = DixonColesModel(max_goals=10)
    dc._rho = 0.0
    np.testing.assert_allclose(dc._joint_matrix(1.7, 1.1), poisson._joint_matrix(1.7, 1.1))


def test_rho_zero_matches_poisson_predictions() -> None:
    rng = np.random.default_rng(9)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _synthetic_dc_league(
        rng, n_teams=8, n_matches=2000, attack=attack, defense=defense, home_adv=0.25, rho=-0.1
    )
    poisson = PoissonModel()
    poisson.fit(matches)

    # DC que comparte el ajuste del Poisson pero con ρ forzado a 0 → mismas probas.
    dc = DixonColesModel()
    dc._teams = poisson._teams
    dc._index = poisson._index
    dc._attack = poisson._attack
    dc._defense = poisson._defense
    dc._home_adv = poisson._home_adv
    dc._mean_defense = poisson._mean_defense
    dc._fitted = True
    dc._rho = 0.0

    for _ in range(15):
        i, j = rng.choice(8, size=2, replace=False)
        m = _match(teams[i], teams[j], 0, 0)
        pp = poisson.predict_proba(m)
        pd = dc.predict_proba(m)
        assert pp.as_dict() == pytest.approx(pd.as_dict())


# --- integración con el walk-forward y registro ------------------------------
def test_integrates_with_walk_forward() -> None:
    rng = np.random.default_rng(17)
    teams = [uuid.uuid4() for _ in range(8)]
    attack = dict(zip(teams, np.linspace(-0.5, 0.5, 8), strict=True))
    defense = dict(zip(teams, np.linspace(-0.3, 0.3, 8), strict=True))
    matches = _synthetic_dc_league(
        rng, n_teams=8, n_matches=400, attack=attack, defense=defense, home_adv=0.25, rho=-0.1
    )
    result = walk_forward(DixonColesModel(), matches, step=5, min_train=40)
    assert result.n_predicted > 0
    for r in result.records:
        assert r.probs.home + r.probs.draw + r.probs.away == pytest.approx(1.0, abs=1e-9)


def test_build_model_registers_dixon_coles() -> None:
    assert isinstance(build_model("dixon_coles"), DixonColesModel)
    assert isinstance(build_model("dc"), DixonColesModel)
