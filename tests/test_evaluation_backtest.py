"""HU 2.1 — Walk-forward: NO-LOOKAHEAD (el test clave), cobertura y re-entreno."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from valuebet.evaluation.backtest import walk_forward
from valuebet.evaluation.model import Match, Probabilities

_T0 = datetime(2023, 8, 1, 12, 0, tzinfo=UTC)
_HOME = uuid.uuid4()
_AWAY = uuid.uuid4()


def _match(kickoff: datetime, *, hg: int = 1, ag: int = 0) -> Match:
    return Match(uuid.uuid4(), kickoff, _HOME, _AWAY, hg, ag)


def _series(n: int, *, equal_pair: bool = False) -> list[Match]:
    """n partidos con saques crecientes; opcionalmente dos simultáneos."""
    out: list[Match] = []
    for i in range(n):
        # Si equal_pair, los índices 1 y 2 comparten el MISMO saque.
        day = 1 if (equal_pair and i == 2) else i
        out.append(_match(_T0 + timedelta(days=day)))
    return out


class SpyModel:
    """Modelo espía: registra qué recibió `fit` antes de cada predicción."""

    def __init__(self) -> None:
        self.fit_calls = 0
        self._last_fit_kickoffs: list[datetime] = []
        # (saque_objetivo, máximo saque en el último fit | None)
        self.checks: list[tuple[datetime, datetime | None]] = []

    def fit(self, matches: Sequence[Match]) -> None:
        self.fit_calls += 1
        self._last_fit_kickoffs = [m.kickoff_utc for m in matches]

    def predict_proba(self, match: Match) -> Probabilities:
        max_fit = max(self._last_fit_kickoffs) if self._last_fit_kickoffs else None
        self.checks.append((match.kickoff_utc, max_fit))
        return Probabilities.uniform()


# --- (a) NO-LOOKAHEAD: el test más importante de la fase ----------------------
def test_no_lookahead_training_strictly_precedes_target() -> None:
    spy = SpyModel()
    matches = _series(8)
    walk_forward(spy, matches, step=1, min_train=1)

    assert spy.checks, "el espía debería haber predicho algo"
    for target_kickoff, max_fit_kickoff in spy.checks:
        assert max_fit_kickoff is not None
        # Ningún partido de entrenamiento tiene saque >= el del objetivo.
        assert max_fit_kickoff < target_kickoff


def test_no_lookahead_excludes_simultaneous_matches() -> None:
    # Con dos partidos al MISMO saque, el segundo no puede entrenar con el primero.
    spy = SpyModel()
    matches = _series(5, equal_pair=True)
    walk_forward(spy, matches, step=1, min_train=1)
    for target_kickoff, max_fit_kickoff in spy.checks:
        assert max_fit_kickoff is None or max_fit_kickoff < target_kickoff


# --- (e) una predicción por evaluable; ninguna sin historia suficiente --------
def test_skips_matches_without_enough_history() -> None:
    spy = SpyModel()
    matches = _series(6)  # saques distintos: pool = índice del partido
    result = walk_forward(spy, matches, step=1, min_train=3)
    # Pools de tamaño 0,1,2 < 3 → 3 omitidos; pools 3,4,5 → 3 predichos.
    assert result.n_finished == 6
    assert result.n_skipped == 3
    assert result.n_predicted == 3
    assert len(result.records) == 3


def test_one_record_per_evaluable_match() -> None:
    spy = SpyModel()
    matches = _series(10)
    result = walk_forward(spy, matches, step=1, min_train=1)
    # min_train=1: el primero (pool vacío) se omite; el resto se predice.
    assert result.n_predicted == 9
    assert result.n_skipped == 1
    predicted_ids = {r.match_id for r in result.records}
    assert len(predicted_ids) == 9


def test_unfinished_matches_are_ignored() -> None:
    spy = SpyModel()
    finished = _series(4)
    pending = Match(uuid.uuid4(), _T0 + timedelta(days=10), _HOME, _AWAY, None, None)
    result = walk_forward(spy, [*finished, pending], step=1, min_train=1)
    assert result.n_finished == 4  # el pendiente no cuenta


# --- re-entrenamiento por lotes (step) ---------------------------------------
def test_step_controls_refit_cadence() -> None:
    spy = SpyModel()
    matches = _series(6)
    result = walk_forward(spy, matches, step=2, min_train=1)
    # 5 predicciones (el primero se omite); re-ajuste cada 2 → fits en la 1ª, 3ª y 5ª.
    assert result.n_predicted == 5
    assert spy.fit_calls == 3
    # Aun reutilizando ajustes, se mantiene el no-lookahead.
    for target_kickoff, max_fit_kickoff in spy.checks:
        assert max_fit_kickoff is None or max_fit_kickoff < target_kickoff


def test_train_window_limits_history(monkeypatch) -> None:
    sizes: list[int] = []

    class SizeModel(SpyModel):
        def fit(self, matches: Sequence[Match]) -> None:
            sizes.append(len(matches))
            super().fit(matches)

    model = SizeModel()
    walk_forward(model, _series(10), train_window=2, step=1, min_train=1)
    # Con ventana deslizante de 2, ningún fit ve más de 2 partidos.
    assert sizes
    assert max(sizes) <= 2


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        walk_forward(SpyModel(), _series(3), step=0)
    with pytest.raises(ValueError):
        walk_forward(SpyModel(), _series(3), min_train=0)
    with pytest.raises(ValueError):
        walk_forward(SpyModel(), _series(3), train_window=0)
