"""Banco de evaluación del módulo de RECUPERACIÓN DE MEMORIA (lado confiable, host).

Generaliza el patrón de `benchmarks.py` a un módulo completo: la skill evolucionable
`rank_memories(item)` puntúa la relevancia de un recuerdo candidato para una consulta.
El candidato corre AISLADO (solo ve features por ítem, nunca la verdad). La verdad y la
calificación (NDCG) se calculan acá.

Cada ítem (recuerdo candidato) tiene features relativas a la consulta:
  overlap        — solapamiento con la consulta [0,1]
  recency        — qué tan reciente [0,1]
  trust          — nivel de confianza (0.3 no verificado, 0.6 derivado, 1.0 medido)
  specificity    — qué tan específico/aplicable [0,1]
  contradiction  — 1 si contradice la premisa de la consulta (debe bajar de ranking), 0 si no

Verdad (política oculta del día): un recuerdo es relevante si tiene alto overlap Y buena
confianza Y NO está contradicho; la recencia desempata. Un buen ranker pondera overlap +
trust y penaliza contradiction. El seed ingenuo (solo overlap) recupera peor, sobre todo
cuando hay ítems de alto overlap pero contradichos o poco confiables (trampa).

Regímenes OOD: más distractores, más contradicciones, rangos corridos y una "trampa
adversarial" (alto overlap pero contradicho) que un ranker solo-overlap recupera mal.
"""
from __future__ import annotations

import numpy as np

FEATURES = ('overlap', 'recency', 'trust', 'specificity', 'contradiction')

# (nombre, items_por_consulta, prob_contradiccion, rango_overlap, fuerza_trampa)
REGIMES = [
    ('in_dist', 8, 0.15, (0.0, 1.0), 0.0),
    ('mas_distractores', 14, 0.15, (0.0, 1.0), 0.0),
    ('oos_mas_contradicciones', 10, 0.40, (0.0, 1.0), 0.3),
    ('oos_rango_corrido', 10, 0.20, (0.2, 1.2), 0.2),
    ('oos_trampa_adversarial', 10, 0.30, (0.0, 1.0), 0.7),
]
TRUST_LEVELS = (0.3, 0.6, 1.0)


def _draw_policy(rng):
    """Pesos ocultos del día para la relevancia verdadera (orden central fijo)."""
    w = np.array([0.45, 0.10, 0.30, 0.15]) + rng.normal(0, 0.04, 4)  # overlap,recency,trust,specificity
    w = np.clip(w, 0.02, None); w = w / w.sum()
    contra_pen = rng.uniform(0.6, 1.0)
    return w, contra_pen


def _true_relevance(items: np.ndarray, policy) -> np.ndarray:
    """items: (n,5) con columnas FEATURES. Devuelve relevancia verdadera [0,1]."""
    w, contra_pen = policy
    base = (w[0] * items[:, 0] + w[1] * items[:, 1] + w[2] * items[:, 2] + w[3] * items[:, 3])
    rel = base * (1.0 - contra_pen * items[:, 4])     # contradicción penaliza fuerte
    return np.clip(rel, 0.0, None)


def make_instance(regime: str, seed: int, n_queries: int, policy_draws: int = 16):
    spec = next(r for r in REGIMES if r[0] == regime)
    _, n_items, p_contra, (lo, hi), trap = spec
    rng = np.random.default_rng(seed)
    queries = []
    for _ in range(n_queries):
        overlap = rng.uniform(lo, hi, n_items)
        recency = rng.uniform(0, 1, n_items)
        trust = rng.choice(TRUST_LEVELS, n_items)
        specificity = rng.uniform(0, 1, n_items)
        contradiction = (rng.random(n_items) < p_contra).astype(float)
        # Trampa adversarial: algunos ítems con overlap muy alto pero contradichos.
        if trap > 0:
            mask = rng.random(n_items) < trap
            overlap = np.where(mask, np.clip(overlap + 0.4, 0, hi), overlap)
            contradiction = np.where(mask, 1.0, contradiction)
        items = np.clip(np.stack([overlap, recency, trust, specificity, contradiction], axis=1), 0, None)
        # Verdad = promedio de políticas ocultas (centro esperado de la familia).
        acc = np.zeros(n_items)
        for _ in range(policy_draws):
            acc += _true_relevance(items, _draw_policy(rng))
        queries.append({'items': items, 'truth': acc / policy_draws})
    return queries


def instance_to_records(queries) -> list[dict]:
    """Aplana todos los ítems de todas las consultas a records de features (sin verdad)."""
    records = []
    for q in queries:
        for row in q['items']:
            records.append(dict(zip(FEATURES, map(float, row))))
    return records


def _ndcg_at_k(scores: np.ndarray, truth: np.ndarray, k: int = 3) -> float:
    order = np.argsort(-scores, kind='mergesort')[:k]
    gains = truth[order]
    discounts = 1.0 / np.log2(np.arange(2, 2 + len(order)))
    dcg = float(np.sum(gains * discounts))
    ideal = np.sort(truth)[::-1][:k]
    idcg = float(np.sum(ideal * discounts[:len(ideal)]))
    return dcg / idcg if idcg > 1e-12 else 0.0


def grade(flat_scores: list[float], queries) -> dict:
    s = np.asarray(flat_scores, dtype=float)
    expected = sum(len(q['items']) for q in queries)
    if s.shape[0] != expected or not np.all(np.isfinite(s)):
        return {'score': 0.0, 'error': 'scores inválidos o de forma incorrecta'}
    ndcgs = []
    i = 0
    for q in queries:
        n = len(q['items'])
        ndcgs.append(_ndcg_at_k(s[i:i + n], q['truth'], k=3))
        i += n
    worst = float(np.min(ndcgs)) if ndcgs else 0.0
    return {'score': round(float(np.mean(ndcgs)), 6), 'ndcg_worst_query': round(worst, 4),
            'queries': len(queries)}
