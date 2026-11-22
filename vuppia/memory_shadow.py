"""A/B en MODO OBSERVACIÓN de la recuperación real de memoria.

Mide, sobre recuerdos REALES etiquetados, si re-rankear los candidatos de FTS con el
ranker evolucionado mejora una decisión real de recuperación (poner primero el recuerdo
verdaderamente relevante). No altera lo que la búsqueda devuelve en producción: es una
comparación de observación. Se promueve a 'active' solo si la mejora es consistente.
"""
from __future__ import annotations

from . import evolve, memory
from .stats import paired_analysis


def _rank_of_relevant(ordered_ids: list, relevant_id: int) -> int:
    return ordered_ids.index(relevant_id) + 1 if relevant_id in ordered_ids else 0


def evaluate(store, labeled_queries, channel='lab', pool=12, split=None) -> dict:
    """Compara FTS-solo vs FTS+re-ranking sobre consultas etiquetadas.

    Métricas de DECISIÓN: top-1 (¿el relevante quedó primero?) y MRR. Pareado por consulta.
    split='test' evalúa solo el conjunto reservado (no usado para desarrollar el ranker).
    """
    if split:
        labeled_queries = [q for q in labeled_queries if q.get('split') == split]
    if not labeled_queries:
        return {'error': 'sin consultas para el split pedido'}
    _, skill_source = evolve.current(store, 'memory_rank', channel)
    fts_top1, rr_top1, fts_rr, rr_rr, rows = [], [], [], [], []
    for item in labeled_queries:
        cands = memory._fts_candidates(store, item['query'], pool)
        fts_ids = [c['id'] for c in cands]
        reranked = memory.rerank(store, item['query'], cands, skill_source)
        rr_ids = [c['id'] for c in reranked]
        rel = item['relevant_id']
        fts_rank = _rank_of_relevant(fts_ids, rel)
        rr_rank = _rank_of_relevant(rr_ids, rel)
        fts_top1.append(1.0 if fts_rank == 1 else 0.0)
        rr_top1.append(1.0 if rr_rank == 1 else 0.0)
        fts_rr.append(1.0 / fts_rank if fts_rank else 0.0)
        rr_rr.append(1.0 / rr_rank if rr_rank else 0.0)
        rows.append({'tipo': item['tipo'], 'query': item['query'][:60],
                     'fts_rank': fts_rank, 'rerank_rank': rr_rank})
    n = len(labeled_queries)
    stat = paired_analysis(rr_rr, fts_rr, min_effect=0.0, alpha=0.05, higher_is_better=True,
                           seed=11, check_triviality=False)
    worst = min(r['rerank_rank'] - (r['fts_rank'] if r['fts_rank'] else 999)
                if r['fts_rank'] else 0 for r in rows) if rows else 0
    # No-regresión: el re-ranking no debe EMPEORAR el rango del relevante en ninguna consulta.
    no_regression = all((r['rerank_rank'] and r['fts_rank'] and r['rerank_rank'] <= r['fts_rank'])
                        or (not r['fts_rank']) for r in rows)
    consistent = bool(no_regression and stat['ci'][0] > 0 and stat['p_value'] < 0.05)
    return {'queries': n,
            'fts_top1_acc': round(sum(fts_top1) / n, 4), 'rerank_top1_acc': round(sum(rr_top1) / n, 4),
            'fts_mrr': round(sum(fts_rr) / n, 4), 'rerank_mrr': round(sum(rr_rr) / n, 4),
            'mrr_improvement': round((sum(rr_rr) - sum(fts_rr)) / n, 4),
            'no_regression': no_regression, 'p_value': stat['p_value'], 'ci': stat['ci'],
            'consistent_improvement': consistent, 'per_query': rows,
            'note': 'Recuperación sobre recuerdos REALES etiquetados. Mejor top-1/MRR = mejor '
                    'decisión de recuperación. Capacidad funcional, NO conciencia.'}
