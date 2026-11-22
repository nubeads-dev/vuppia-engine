"""Banco de evaluación de skills — lado CONFIABLE (corre en el host, nunca junto al candidato).

Diseño de aislamiento (corrección #1 de la auditoría 3.0→3.1):
- El candidato se ejecuta en SU PROPIO contenedor y recibe SOLO vectores de features.
  Nunca ve la verdad, ni la semilla, ni el harness, ni los coeficientes.
- La "verdad" se genera acá, en el host, y la calificación (error) se calcula acá.
  El candidato devuelve números; no puede fabricar su puntaje ni leer datos reservados.
- La verdad NO es una fórmula fija conocida por el generador: es una POLÍTICA OCULTA
  del día, muestreada de una familia con coeficientes aleatorios (de una semilla secreta
  comprometida por hash antes de evaluar). No hay constantes que memorizar.

Medición genuina fuera de distribución (corrección #5):
- Varios REGÍMENES estructuralmente distintos (lineal, saturante, con interacción,
  y rangos de entrada OOD). La mejora debe sostenerse en el PEOR régimen, no solo en promedio.

Significado: `score_hypothesis` ordena hipótesis por evidence/falsifiability/feasibility/
importance. La familia es monótona creciente en las cuatro, con un orden de énfasis
central (evidence≈falsifiability ≥ feasibility ≥ importance) y no linealidad suave. Una
función que captura el centro de la familia y es monótona generaliza; una que memoriza
constantes de un seed, o es constante, falla en OOD.
"""
from __future__ import annotations

import numpy as np

FEATURES = ('evidence', 'falsifiability', 'feasibility', 'importance')

# Régimenes: (nombre, rango de entrada, tipo de no linealidad, fuerza de interacción)
REGIMES = [
    ('lineal_in_dist', (0.0, 1.0), 'none', 0.0),
    ('saturante_in_dist', (0.0, 1.0), 'sat', 0.0),
    ('interaccion_in_dist', (0.0, 1.0), 'sat', 0.6),
    ('oos_rango_ancho', (-0.3, 1.3), 'sat', 0.3),
    ('oos_sesgado_alto', (0.4, 1.4), 'sat', 0.2),
    ('oos_extrapolacion', (-0.6, 1.6), 'sat', 0.4),
]

# Prior central de la familia (oculto al generador): orden de énfasis fijo, magnitud variable.
_CENTER = np.array([0.30, 0.30, 0.25, 0.15])


def _draw_policy(rng: np.random.Generator):
    """Una política oculta del día: pesos cerca del centro + no linealidad e interacción."""
    w = _CENTER + rng.normal(0, 0.05, 4)
    w = np.clip(w, 0.02, None)
    w = w / w.sum()
    sat = rng.uniform(0.6, 1.4)         # curvatura saturante
    gamma = rng.uniform(0.85, 1.15)     # exponente monótono
    inter = rng.normal(0, 1, (4, 4))
    inter = (inter + inter.T) / 2
    np.fill_diagonal(inter, 0.0)
    return w, sat, gamma, inter


def _truth(items: np.ndarray, policy, interaction_strength: float, nonlin: str) -> np.ndarray:
    w, sat, gamma, inter = policy
    lin = items @ w
    base = np.sign(lin) * np.abs(lin) ** gamma
    if nonlin == 'sat':
        base = np.tanh(sat * base) / np.tanh(sat)      # saturación suave, normalizada
    if interaction_strength:
        quad = np.einsum('ni,ij,nj->n', items, inter, items) / 4.0
        base = base + interaction_strength * 0.15 * quad
    y = 100.0 * base
    return np.clip(y, 0.0, 100.0)


def make_instance(regime: str, seed: int, n: int, policy_draws: int = 1):
    """Devuelve (items_features, truth) para un régimen y semilla. items SIN etiquetas."""
    spec = next(r for r in REGIMES if r[0] == regime)
    _, (lo, hi), nonlin, inter_strength = spec
    rng = np.random.default_rng(seed)
    items = rng.uniform(lo, hi, (n, 4))
    # Promediar varias políticas ocultas = verdad = centro esperado de la familia del día.
    acc = np.zeros(n)
    for _ in range(policy_draws):
        acc += _truth(items, _draw_policy(rng), inter_strength, nonlin)
    truth = acc / policy_draws
    return items, truth


def items_to_records(items: np.ndarray) -> list[dict]:
    return [dict(zip(FEATURES, map(float, row))) for row in items]


def grade(scores: list[float], truth: np.ndarray) -> dict:
    s = np.asarray(scores, dtype=float)
    if s.shape != truth.shape or not np.all(np.isfinite(s)):
        return {'score': 0.0, 'error': 'scores invalidos o de forma incorrecta'}
    if np.any(s < -1e6) or np.any(s > 1e6):
        return {'score': 0.0, 'error': 'scores fuera de rango plausible'}
    ae = np.abs(s - truth)
    return {'score': round(float(max(0.0, 1.0 - ae.mean() / 100.0)), 6),
            'mae': round(float(ae.mean()), 4), 'worst_abs_err': round(float(ae.max()), 4),
            'n': int(truth.size)}
