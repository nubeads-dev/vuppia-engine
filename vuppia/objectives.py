"""Objetivos a corto, mediano y largo plazo, con progreso calculado de datos REALES.

No son textos decorativos: cada objetivo define una métrica medible y su progreso se computa
de la base (experimentos, evolución, memoria). Así "¿en qué mejoraste?" se responde con hechos.
La automejora es acumulativa: las métricas miran lo verificado, no lo propuesto.
"""
from __future__ import annotations

from .store import Store


def _count(store: Store, sql, args=()):
    return store.one(sql, args)[0]


def progress(store: Store) -> dict:
    """Métricas de progreso reales (acumulativas)."""
    # Antes contaba filas de skill_evals, que crecían cada vez que el monitoreo re-evaluaba el
    # mismo código. Ahora cuenta versiones DISTINTAS incorporadas con evaluación independiente.
    from . import evolve as _ev
    verified_improvements = _ev.verified_improvements(store)
    modules_evolved = _count(store, "SELECT COUNT(DISTINCT skill) FROM skill_releases WHERE "
                                    "channel='production' AND kind='promotion' AND holdout_eval_id IS NOT NULL")
    supported = _count(store, "SELECT COUNT(*) FROM memories WHERE kind='finding' AND status='supported'")
    refuted = _count(store, "SELECT COUNT(*) FROM memories WHERE kind='finding' AND status='refuted'")
    experiments = _count(store, 'SELECT COUNT(*) FROM hypotheses')
    research_runs = _count(store, 'SELECT COUNT(*) FROM research_runs') \
        if store.one("SELECT 1 FROM sqlite_master WHERE name='research_runs'") else 0
    in_production = _count(store, "SELECT COUNT(*) FROM skill_pointers WHERE channel='production'")
    return {
        'experimentos_corridos': experiments,
        'hallazgos_comprobados': supported,
        'hallazgos_refutados': refuted,
        'mejoras_de_codigo_verificadas': verified_improvements,
        'modulos_evolucionados': modules_evolved,
        'modulos_en_produccion': in_production,
        'investigaciones_externas': research_runs,
    }


def objectives(store: Store) -> list[dict]:
    """Los tres horizontes, cada uno con su métrica y progreso real."""
    p = progress(store)
    # Corto: validar la memoria con datos reales antes de activarla.
    real_labeled = _count(store, "SELECT COUNT(*) FROM memories WHERE provenance LIKE 'realquery:%'") \
        if store.one("SELECT 1 FROM sqlite_master WHERE name='memories'") else 0
    short = {'horizonte': 'corto', 'objetivo': 'Validar la memoria con 100–200 consultas reales '
             'etiquetadas y activar el ranker solo si supera el test reservado',
             'metrica': 'consultas reales etiquetadas', 'actual': real_labeled, 'meta': 150,
             'estado': 'en curso' if real_labeled < 150 else 'cumplido'}
    # Mediano: evolucionar módulos más allá de la función (memoria/razonamiento/planificación).
    mid = {'horizonte': 'mediano', 'objetivo': 'Evolucionar módulos completos (memoria, razonamiento, '
           'planificación, herramientas) con mejoras verificadas',
           'metrica': 'módulos evolucionados verificados', 'actual': p['modulos_evolucionados'], 'meta': 4,
           'estado': 'en curso'}
    # Largo: investigar la conciencia sin autoengaño (nunca "cumplido": es un programa abierto).
    lng = {'horizonte': 'largo', 'objetivo': 'Investigar rigurosamente la posibilidad de conciencia '
           'artificial, sin afirmarla sin evidencia',
           'metrica': 'experimentos falsables corridos', 'actual': p['experimentos_corridos'], 'meta': None,
           'estado': 'programa abierto (no se declara cumplido)'}
    return [short, mid, lng]
