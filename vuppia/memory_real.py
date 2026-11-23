"""Banco de evaluación de recuperación de memoria sobre DATOS REALES (lado confiable, host).

A diferencia de `memory_benchmarks.py` (sintético, con una fórmula oculta que un candidato
puede terminar reproduciendo), acá la verdad NO sale de ninguna fórmula sobre las features:
sale de la PROCEDENCIA real de cada recuerdo.

  Consulta   : la pregunta (texto natural) de un experimento real ya ejecutado.
  Candidatos : lo que devuelve la búsqueda FTS real para esa pregunta (el mismo camino que usa
               el planificador), sin el hallazgo del propio experimento.
  Relevancia : 2 si el recuerdo es un hallazgo medido de otro experimento del MISMO protocolo
               y los mismos brazos (misma familia de hipótesis); 1 si es del mismo protocolo;
               0 en cualquier otro caso (otros protocolos, notas web sin verificar, etc.).
  Métrica    : NDCG@10 por consulta.

El candidato corre aislado y solo recibe las 5 features reales (`memory.features_for`); nunca
ve la pregunta, el contenido, el protocolo ni la relevancia. Los experimentos se parten en
DESARROLLO y RESERVA por hash de su id: la evaluación de candidatos usa desarrollo y la
repetición independiente usa reserva, que el generador de código nunca ve ni resumida.

Limitación declarada: mide una decisión real y acotada (qué recuerdos propios poner primero
al planificar). No mide comprensión ni conciencia.
"""
from __future__ import annotations

import hashlib
import math
import random

from . import memory

REGIMES = ['real_a', 'real_b', 'real_c']     # tres tercios disjuntos de experimentos
POOL = 30
MIN_QUERIES = 12                              # por régimen y partición, para considerarlo disponible
K = 10

_STORE = None
_SPLIT = 'dev'
_CACHE: dict = {}


def bind(store, split: str = 'dev') -> None:
    """La evaluación corre de a una en el worker: se fija el store y la partición antes."""
    global _STORE, _SPLIT
    _STORE = store
    _SPLIT = 'holdout' if split == 'holdout' else 'dev'


def _h(hid: int) -> int:
    return int(hashlib.sha256(f'vuppia-real:{hid}'.encode()).hexdigest()[:8], 16)


def _split_of(hid: int) -> str:
    return 'holdout' if _h(hid) % 2 else 'dev'


def _regime_of(hid: int) -> str:
    return REGIMES[(_h(hid) // 2) % 3]


def _experiments(store) -> dict:
    """hid -> (pregunta, protocolo, brazos) de experimentos con hallazgo medido."""
    rows = store.q("SELECT h.id,h.question,h.protocol,h.test_arm,h.control_arm FROM hypotheses h "
                   "WHERE h.status='analyzed' AND EXISTS(SELECT 1 FROM memories m WHERE "
                   "m.provenance='experiment:'||h.id) ORDER BY h.id")
    return {r[0]: (r[1], r[2], (r[3], r[4])) for r in rows}


def _queries(store, regime: str, split: str) -> list:
    """Consultas válidas (con al menos un relevante y un irrelevante en el pool real)."""
    n = store.one('SELECT COUNT(*) FROM memories')[0]
    key = (str(store.path), regime, split, n)
    if key in _CACHE:
        return _CACHE[key]
    exps = _experiments(store)
    out = []
    for hid, (question, protocol, arms) in exps.items():
        if _split_of(hid) != split or _regime_of(hid) != regime:
            continue
        pool = [c for c in memory._fts_candidates(store, question, POOL)
                if c.get('provenance') != f'experiment:{hid}']
        if len(pool) < 4:
            continue
        gains = []
        for c in pool:
            g = 0
            prov = c.get('provenance') or ''
            if prov.startswith('experiment:'):
                try:
                    other = exps.get(int(prov.split(':', 1)[1]))
                except ValueError:
                    other = None
                if other and other[1] == protocol:
                    g = 2 if other[2] == arms else 1
            gains.append(g)
        if max(gains) == 0 or min(gains) == max(gains):
            continue
        qterms = memory._terms(question)
        feats = [memory.features_for(store, qterms, c) for c in pool]
        out.append({'hid': hid, 'features': feats, 'gains': gains})
    for k in [k for k in _CACHE if k[0] == key[0] and k[3] != n]:
        del _CACHE[k]                                   # la base creció: se descarta lo viejo
    _CACHE[key] = out
    return out


def available(store=None) -> bool:
    store = store or _STORE
    if store is None:
        return False
    try:
        return all(len(_queries(store, r, s)) >= MIN_QUERIES for r in REGIMES for s in ('dev', 'holdout'))
    except Exception:                                   # noqa: BLE001
        return False


def make_instance(regime: str, seed: int, n_queries: int = 40):
    qs = _queries(_STORE, regime, _SPLIT)
    rng = random.Random(seed)
    return rng.sample(qs, min(n_queries, len(qs)))


def instance_to_records(inst) -> list:
    return [f for q in inst for f in q['features']]


def _ndcg(scores, gains) -> float:
    order = sorted(range(len(gains)), key=lambda i: scores[i], reverse=True)[:K]
    dcg = sum((2 ** gains[i] - 1) / math.log2(pos + 2) for pos, i in enumerate(order))
    ideal = sorted(gains, reverse=True)[:K]
    idcg = sum((2 ** g - 1) / math.log2(pos + 2) for pos, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def grade(scores, inst) -> dict:
    """NDCG@10 medio del candidato y, como referencia, el del orden FTS que hoy se devuelve."""
    i = 0
    cand, fts = [], []
    for q in inst:
        n = len(q['gains'])
        cand.append(_ndcg(scores[i:i + n], q['gains']))
        fts.append(_ndcg([-p for p in range(n)], q['gains']))      # orden FTS = orden del pool
        i += n
    m = len(cand) or 1
    return {'score': sum(cand) / m, 'fts': sum(fts) / m, 'queries': len(cand)}


def dev_diagnostics(store) -> dict:
    """Resumen SOLO de la partición de desarrollo, para orientar al generador de código:
    media de cada feature entre recuerdos relevantes e irrelevantes. No incluye la reserva."""
    rel = {}; irr = {}; nr = ni = 0
    for r in REGIMES:
        for q in _queries(store, r, 'dev'):
            for f, g in zip(q['features'], q['gains']):
                tgt = rel if g > 0 else irr
                for k, v in f.items():
                    tgt[k] = tgt.get(k, 0.0) + float(v)
                if g > 0:
                    nr += 1
                else:
                    ni += 1
    return {'relevantes': {k: round(v / max(nr, 1), 3) for k, v in rel.items()},
            'irrelevantes': {k: round(v / max(ni, 1), 3) for k, v in irr.items()},
            'n_relevantes': nr, 'n_irrelevantes': ni}


def summary(store) -> dict:
    """Tamaño del banco real por régimen y partición (diagnóstico, sin sandbox)."""
    out = {'experimentos_con_hallazgo': len(_experiments(store)), 'por_regimen': {}}
    for r in REGIMES:
        out['por_regimen'][r] = {s: len(_queries(store, r, s)) for s in ('dev', 'holdout')}
    out['disponible'] = available(store)
    return out
