"""Planificador guiado por la Primera Ley como POLÍTICA EXPLÍCITA Y VERIFICABLE.

La Primera Ley deja de ser un string: es `first_law_policy`, una función que puntúa
cada experimento candidato por su valor de información hacia los dos objetivos
—investigar la conciencia artificial y mejorar el propio software— y que queda
registrada (política + ranking + decisión) para poder auditarla.

El planificador prioriza candidatos con la SKILL EVOLUCIONABLE `score_hypothesis`
(la misma función que evoluciona `evolve.py`). Así la autorreprogramación es útil:
una mejor función de evaluación produce mejores decisiones del planificador. Eso se
demuestra por A/B midiendo el ARREPENTIMIENTO (regret) contra un oráculo independiente.

Nada de esto mide conciencia: es triage de experimentos, una capacidad funcional.
"""
from __future__ import annotations

import numpy as np

from . import benchmarks, evolve
from .protocols import PROTOCOLS
from .store import Store, canonical, utc

# Pesos de la Primera Ley: cuánto pondera cada objetivo al fijar la "importancia" de un
# experimento. Explícitos y auditables; se registran en cada decisión.
FIRST_LAW = {
    'consciousness_investigation': 0.6,   # protocolos que sondean autoacceso / integración
    'self_improvement': 0.4,              # experimentos que ejercitan la automejora del software
}
# Qué objetivo toca cada protocolo (política declarada, verificable).
PROTOCOL_GOAL = {
    'introspection_probe': 'consciousness_investigation',
    'llm_self_prediction': 'consciousness_investigation',
    # Monitoreo metacognitivo (indicador HOT-2): distinguir aciertos de errores propios.
    'metacog_head': 'consciousness_investigation',
    'confidence_error_detection': 'consciousness_investigation',
    'llm_verbal_confidence': 'self_improvement',
}


def first_law_policy(candidate: dict, store: Store) -> dict:
    """Mapea un experimento candidato a las 4 features de la Primera Ley. Verificable:
    dadas las mismas entradas devuelve siempre lo mismo y queda registrada."""
    protocol = candidate['protocol']
    spec = PROTOCOLS[protocol]
    # feasibility: más barato = más factible (coste ≈ n_units * trials).
    cost = candidate['n_units'] * candidate['params'].get('trials', candidate['params'].get('n_train', 1000))
    feasibility = float(np.clip(1.0 - np.log10(max(cost, 1)) / 7.0, 0.0, 1.0))
    # falsifiability: poder estadístico esperado (efecto esperado / ruido).
    falsifiability = float(np.clip(spec['sd_guess'] / max(candidate['min_effect'], 1e-6) / 3.0, 0.0, 1.0))
    # evidence: apoyo previo (hallazgos 'supported' del mismo protocolo en memoria).
    prior = store.one("SELECT COUNT(*) FROM memories WHERE kind='finding' AND status='supported' "
                      "AND content LIKE ?", ('%' + protocol + '%',))[0]
    evidence = float(np.clip(prior / 5.0, 0.0, 1.0))
    # importance: alineación con la Primera Ley (objetivo del protocolo).
    goal = PROTOCOL_GOAL.get(protocol, 'self_improvement')
    importance = FIRST_LAW[goal]
    return {'evidence': evidence, 'falsifiability': falsifiability,
            'feasibility': feasibility, 'importance': importance}


def oracle_value(features: dict) -> float:
    """Valor VERDADERO de informatividad de un candidato, independiente de la skill.
    Usa la verdad de la familia (centro), que la skill solo aproxima."""
    x = np.array([[features['evidence'], features['falsifiability'],
                   features['feasibility'], features['importance']]])
    # Promediar políticas ocultas de la familia = valor esperado (determinista por seed fijo).
    rng = np.random.default_rng(20261116)
    acc = 0.0
    for _ in range(64):
        acc += float(benchmarks._truth(x, benchmarks._draw_policy(rng), 0.0, 'sat')[0])
    return acc / 64


def enumerate_candidates(store: Store, pool_seed: int, size: int = 8, kinds=None) -> list[dict]:
    """Candidatos reales: combinaciones de protocolo/parámetros con sus features.

    Los experimentos candidatos cubren perfiles variados en las cuatro dimensiones
    (unos con más apoyo previo, otros más exploratorios; unos baratos, otros caros;
    unos más o menos falsables). `importance` la fija la Primera Ley según el objetivo
    del protocolo. Esta diversidad es la que hace que la calidad de la función de
    evaluación cambie qué experimento se elige (si no, una sola feature domina y todas
    las funciones coinciden)."""
    rng = np.random.default_rng(pool_seed)
    cands = []
    protos = [p for p in PROTOCOLS if PROTOCOLS[p]['kind'] in kinds] if kinds else list(PROTOCOLS)
    if not protos:
        protos = list(PROTOCOLS)
    for i in range(size):
        protocol = protos[rng.integers(len(protos))]
        spec = PROTOCOLS[protocol]
        params = {}
        for k, (lo, hi, default) in spec['params'].items():
            v = rng.uniform(lo, hi)
            params[k] = int(v) if k in spec['int_params'] else round(float(v), 3)
        min_effect = max(spec['sd_guess'] * rng.uniform(0.3, 0.6), 0.01)
        cand = {'id': i, 'protocol': protocol, 'params': params,
                'n_units': int(rng.integers(8, 24)), 'min_effect': min_effect}
        base = first_law_policy(cand, store)
        # Perfil del experimento: evidencia previa, falsabilidad y factibilidad con dispersión
        # realista (trade-offs entre experimentos). importance la manda la Primera Ley.
        cand['features'] = {
            'evidence': round(float(np.clip(base['evidence'] + rng.beta(1.5, 2.5), 0, 1)), 3),
            'falsifiability': round(float(np.clip(0.4 * base['falsifiability'] + 0.6 * rng.beta(2, 2), 0, 1)), 3),
            'feasibility': round(float(np.clip(0.4 * base['feasibility'] + 0.6 * rng.beta(2, 2), 0, 1)), 3),
            'importance': base['importance'],
        }
        cand['oracle'] = oracle_value(cand['features'])
        cands.append(cand)
    return cands


def _score_features(skill_source: str, feature_list: list[dict]) -> list[float]:
    """Puntúa con la skill, AISLADA en el sandbox (código evolucionado = no confiable)."""
    return evolve._run_candidate(skill_source, feature_list)


def rank(store: Store, candidates: list[dict], skill_source: str) -> list[dict]:
    scores = _score_features(skill_source, [c['features'] for c in candidates])
    ordered = sorted(zip(candidates, scores), key=lambda t: t[1], reverse=True)
    return [{'candidate': c, 'priority': s} for c, s in ordered]


def choose(store: Store, skill_channel: str = 'lab', pool_seed: int | None = None, kinds=None) -> dict:
    """Elige el próximo experimento según la Primera Ley + la skill. Registra la decisión."""
    import secrets
    pool_seed = pool_seed if pool_seed is not None else secrets.randbelow(2**31)
    sha, source = evolve.current(store, 'hypothesis', channel=skill_channel)
    cands = enumerate_candidates(store, pool_seed, kinds=kinds)
    ranked = rank(store, cands, source)
    chosen = ranked[0]['candidate']
    oracle_best = max(cands, key=lambda c: c['oracle'])
    regret = round(oracle_best['oracle'] - chosen['oracle'], 4)
    with store.tx() as db:
        db.execute('INSERT INTO planner_decisions(at,policy,skill_sha,chosen,ranking,regret,oracle_best,note) '
                   'VALUES(?,?,?,?,?,?,?,?)',
                   (utc(), canonical(FIRST_LAW), sha, canonical({'protocol': chosen['protocol'],
                    'params': chosen['params'], 'features': chosen['features']}),
                    canonical([{'protocol': r['candidate']['protocol'], 'priority': round(r['priority'], 3)}
                               for r in ranked]), regret,
                    canonical({'protocol': oracle_best['protocol']}),
                    'Triage de experimentos por la Primera Ley. No mide conciencia.'))
    return {'chosen': chosen, 'priority': ranked[0]['priority'], 'regret': regret,
            'skill_sha': sha, 'pool_seed': pool_seed}


def ab_test(store: Store, source_a: str, source_b: str, trials: int = 60) -> dict:
    """A/B: ¿la skill B toma mejores decisiones de planificación que la A? Mide el regret
    promedio en pools aleatorios de candidatos. Menor regret = mejores decisiones.

    Eficiente: puntúa TODOS los candidatos de todos los pools en una sola corrida por
    skill (2 contenedores en total), luego arma la decisión por pool."""
    import secrets
    pools = []
    flat_features = []
    for _ in range(trials):
        cands = enumerate_candidates(store, secrets.randbelow(2**31))
        idx0 = len(flat_features)
        flat_features.extend(c['features'] for c in cands)
        pools.append((cands, idx0, len(cands)))
    scores_a = _score_features(source_a, flat_features)
    scores_b = _score_features(source_b, flat_features)
    regrets_a, regrets_b = [], []
    for cands, idx0, n in pools:
        oracle_best = max(c['oracle'] for c in cands)
        sa = scores_a[idx0:idx0 + n]; sb = scores_b[idx0:idx0 + n]
        top_a = cands[max(range(n), key=lambda i: sa[i])]
        top_b = cands[max(range(n), key=lambda i: sb[i])]
        regrets_a.append(oracle_best - top_a['oracle'])
        regrets_b.append(oracle_best - top_b['oracle'])
    from .stats import paired_analysis
    # Menor regret es mejor → comparamos (regret_A - regret_B), mejor si > 0.
    diff = [a - b for a, b in zip(regrets_a, regrets_b)]
    stat = paired_analysis([d for d in diff], [0.0] * len(diff), min_effect=0.0, alpha=0.05,
                           higher_is_better=True, seed=3, check_triviality=False)
    return {'trials': trials,
            'mean_regret_a': round(float(np.mean(regrets_a)), 4),
            'mean_regret_b': round(float(np.mean(regrets_b)), 4),
            'regret_reduction': round(float(np.mean(regrets_a) - np.mean(regrets_b)), 4),
            'b_better': bool(stat['ci'][0] > 0 and stat['p_value'] < 0.05),
            'ci_reduction': stat['ci'], 'p_value': stat['p_value'],
            'note': 'Mejor triage de experimentos por una mejor función de evaluación. '
                    'Es capacidad funcional, NO conciencia.'}
