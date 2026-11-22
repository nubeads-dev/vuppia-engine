"""A/B de DECISIÓN de recuperación de memoria, análogo al regret del planificador.

Mide una métrica de DECISIÓN distinta de la de entrenamiento (NDCG): la exactitud
top-1, es decir, con qué frecuencia el ranker pone PRIMERO el recuerdo verdaderamente
más relevante, sobre consultas OOD frescas. Así se demuestra que evolucionar el módulo
de memoria mejora una decisión real, no solo el puntaje del banco. No mide conciencia.
"""
from __future__ import annotations

import secrets

import numpy as np

from . import evolve, memory_benchmarks
from .stats import paired_analysis
from .store import Store


def _top1_accuracy(store: Store, source: str, pools) -> list[float]:
    """Por consulta: 1 si el ítem rankeado primero es el de mayor relevancia verdadera."""
    flat = []
    for queries in pools:
        flat.extend(memory_benchmarks.instance_to_records(queries))
    scores = evolve._run_candidate(source, flat, 'rank_memories')
    hits = []
    i = 0
    for queries in pools:
        for q in queries:
            n = len(q['items'])
            s = np.asarray(scores[i:i + n]); i += n
            hits.append(float(int(np.argmax(s) == np.argmax(q['truth']))))
    return hits


def retrieval_ab(store: Store, source_a: str, source_b: str, trials: int = 30) -> dict:
    """Genera consultas OOD frescas (régimen de trampa adversarial, el más exigente) y
    compara la exactitud top-1 de dos rankers. Menor error = mejor decisión."""
    pools = [memory_benchmarks.make_instance('oos_trampa_adversarial', secrets.randbelow(2**31), 4)
             for _ in range(trials)]
    acc_a = _top1_accuracy(store, source_a, pools)
    acc_b = _top1_accuracy(store, source_b, pools)
    stat = paired_analysis(acc_b, acc_a, min_effect=0.0, alpha=0.05, higher_is_better=True,
                           seed=5, check_triviality=False)
    return {'trials_queries': len(acc_a),
            'top1_acc_a': round(float(np.mean(acc_a)), 4),
            'top1_acc_b': round(float(np.mean(acc_b)), 4),
            'improvement': round(float(np.mean(acc_b) - np.mean(acc_a)), 4),
            'b_better': bool(stat['ci'][0] > 0 and stat['p_value'] < 0.05),
            'ci': stat['ci'], 'p_value': stat['p_value'],
            'note': 'Mejor recuperación top-1 en consultas OOD por un mejor ranker. '
                    'Capacidad funcional, NO conciencia.'}
