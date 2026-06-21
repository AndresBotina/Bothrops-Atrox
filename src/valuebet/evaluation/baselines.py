"""Modelos baseline: la VARA DE REFERENCIA del evaluador (HU 2.1).

Un baseline es el "alumno tonto": probabilidades derivadas de frecuencias
históricas, sin modelar fuerza de equipos ni goles esperados. Existen para fijar
el mínimo que cualquier modelo serio (Dixon-Coles, Fase 3) debe superar en
calibración (Brier/log loss). Si un modelo no le gana al baseline, no aporta.

Ambos implementan `PredictionModel` (ver `model.py`) y respetan el no-lookahead:
sólo usan los partidos que el evaluador les pasa en `fit`, todos anteriores al
saque del partido a predecir.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence

from valuebet.evaluation.model import OUTCOMES, Match, Outcome, Probabilities


class HomeAdvantageBaseline:
    """Probabilidades fijas = tasa histórica global de local/empate/visitante.

    Captura la "ventaja de campo" promedio del set de entrenamiento. Predice lo
    mismo para todos los partidos: no distingue equipos. Sin datos de
    entrenamiento cae a la distribución uniforme (1/3 cada uno).
    """

    def __init__(self) -> None:
        self._probs: Probabilities = Probabilities.uniform()

    def fit(self, matches: Sequence[Match]) -> None:
        finished = [m for m in matches if m.is_finished]
        if not finished:
            self._probs = Probabilities.uniform()
            return
        counts: Counter[str] = Counter(m.outcome for m in finished)
        n = len(finished)
        self._probs = Probabilities(
            home=counts["home"] / n,
            draw=counts["draw"] / n,
            away=counts["away"] / n,
        )

    def predict_proba(self, match: Match) -> Probabilities:  # noqa: ARG002 (no usa el partido)
        return self._probs


class TeamFrequencyBaseline:
    """Frecuencias por equipo, suavizadas hacia la distribución global.

    Para cada partido combina dos evidencias:
      * el registro del LOCAL jugando EN CASA (su distribución 1X2 como local), y
      * el registro del VISITANTE jugando FUERA (su distribución 1X2 como
        visitante),
    y promedia ambas. Un equipo con poco historial se "encoge" hacia la tasa
    global vía suavizado aditivo (Laplace), evitando probabilidades extremas con
    muestras minúsculas. Equipos desconocidos → distribución global.
    """

    def __init__(self, smoothing: float = 4.0) -> None:
        if smoothing < 0:
            raise ValueError("smoothing debe ser >= 0")
        self._smoothing = smoothing
        self._global: Probabilities = Probabilities.uniform()
        # Conteos de desenlace 1X2 (perspectiva del partido) por equipo.
        self._home_counts: dict[object, Counter[str]] = defaultdict(Counter)
        self._away_counts: dict[object, Counter[str]] = defaultdict(Counter)

    def fit(self, matches: Sequence[Match]) -> None:
        finished = [m for m in matches if m.is_finished]
        self._home_counts = defaultdict(Counter)
        self._away_counts = defaultdict(Counter)
        if not finished:
            self._global = Probabilities.uniform()
            return
        global_counts: Counter[str] = Counter()
        for m in finished:
            outcome = m.outcome
            global_counts[outcome] += 1
            self._home_counts[m.home_team_id][outcome] += 1
            self._away_counts[m.away_team_id][outcome] += 1
        n = len(finished)
        self._global = Probabilities(
            home=global_counts["home"] / n,
            draw=global_counts["draw"] / n,
            away=global_counts["away"] / n,
        )

    def _smoothed(self, counts: Counter[str]) -> tuple[float, float, float]:
        """Distribución suavizada hacia la global (Laplace ponderado)."""
        n = sum(counts.values())
        s = self._smoothing
        denom = n + s
        prior = self._global
        return tuple(  # type: ignore[return-value]
            (counts[o] + s * prior.get(o)) / denom for o in OUTCOMES
        )

    def predict_proba(self, match: Match) -> Probabilities:
        home_dist = self._smoothed(self._home_counts[match.home_team_id])
        away_dist = self._smoothed(self._away_counts[match.away_team_id])
        # Promedio de dos distribuciones normalizadas → ya normalizado.
        avg = [(h + a) / 2.0 for h, a in zip(home_dist, away_dist, strict=True)]
        return Probabilities(home=avg[0], draw=avg[1], away=avg[2])


# Registro de modelos disponibles para la CLI (nombre → fábrica).
# Incluye los baselines (vara de referencia) y modelos reales (Poisson, Fase 3).
def _home_advantage() -> HomeAdvantageBaseline:
    return HomeAdvantageBaseline()


def _team_frequency() -> TeamFrequencyBaseline:
    return TeamFrequencyBaseline()


def _require_modeling(model_name: str):
    """Import perezoso de la capa `modeling` (extra opcional numpy/scipy)."""
    try:
        from valuebet.modeling.dixon_coles import DixonColesModel
        from valuebet.modeling.poisson import PoissonModel
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise ImportError(
            f"el modelo '{model_name}' requiere el extra 'modeling' (numpy/scipy); "
            "instálalo con 'uv sync --extra modeling'."
        ) from exc
    return PoissonModel, DixonColesModel


def _poisson():
    poisson_cls, _ = _require_modeling("poisson")
    return poisson_cls()


def _dixon_coles():
    _, dc_cls = _require_modeling("dixon_coles")
    return dc_cls()


BASELINES: dict[str, object] = {
    "baseline": _home_advantage,  # alias por defecto
    "home-advantage": _home_advantage,
    "team-frequency": _team_frequency,
    "poisson": _poisson,
    "dixon_coles": _dixon_coles,
    "dc": _dixon_coles,  # alias corto
}


def build_model(name: str):
    """Instancia un modelo por nombre. Lanza KeyError con los nombres válidos."""
    try:
        factory = BASELINES[name]
    except KeyError:
        valid = ", ".join(sorted(BASELINES))
        raise KeyError(f"modelo desconocido '{name}'; disponibles: {valid}") from None
    return factory()  # type: ignore[operator]


# `Outcome` se re-exporta por conveniencia de tipado en otros módulos.
__all__ = [
    "HomeAdvantageBaseline",
    "TeamFrequencyBaseline",
    "BASELINES",
    "build_model",
    "Outcome",
]
