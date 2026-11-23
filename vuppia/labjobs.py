"""Trabajos de laboratorio pedidos por el operador y ejecutados por el worker (que es quien
tiene el sandbox). El pedido y el resultado viajan por kv; ninguno modifica producción.

  real_bench     : tamaño del banco de datos reales y NDCG@10 (desarrollo y reserva) del orden
                   de búsqueda actual, de la semilla y de las versiones en lab y producción.
  rollback_drill : simulacro en una base TEMPORAL y descartable: se incorpora a propósito una
                   versión mala y se verifica que el monitoreo la detecte y revierta sola,
                   usando el sandbox real del servidor.
"""
from __future__ import annotations

import json
import os
import tempfile

from .store import Store, utc


def request(store: Store, job: str) -> None:
    with store.tx() as db:
        db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')
        db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('lab_request',?)", (job,))


def _save(store: Store, job: str, result: dict) -> None:
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)",
                   ('lab_result:' + job, json.dumps({'at': utc(), 'job': job, **result}, ensure_ascii=False)))
        db.execute("DELETE FROM kv WHERE k='lab_request'")


def run_pending(store: Store):
    row = store.one("SELECT v FROM kv WHERE k='lab_request'")
    if not row or not row[0]:
        return None
    job = row[0]
    try:
        fn = {'real_bench': real_bench, 'rollback_drill': rollback_drill}.get(job)
        res = fn(store) if fn else {'error': 'trabajo desconocido'}
    except Exception as exc:                            # noqa: BLE001
        res = {'error': repr(exc)[:300]}
    _save(store, job, res)
    return job


def real_bench(store: Store) -> dict:
    from . import evolve, memory_real
    out = {'banco': memory_real.summary(store)}
    if not out['banco']['disponible']:
        return out
    out['diagnostico_desarrollo'] = memory_real.dev_diagnostics(store)
    versions = {'semilla': evolve.seed_source('memory_rank')}
    for ch in ('lab', 'production'):
        r = store.one('SELECT v.source,v.sha FROM skill_pointers p JOIN skill_versions v ON v.skill=p.skill '
                      "AND v.sha=p.sha WHERE p.skill='memory_rank' AND p.channel=?", (ch,))
        if r:
            versions[f'{ch}:{r[1][:12]}'] = r[0]
    spec = evolve.skill_spec('memory_rank')
    res = {}
    for split in ('dev', 'holdout'):
        memory_real.bind(store, split)
        insts = [memory_real.make_instance(reg, 7, 10 ** 6) for reg in memory_real.REGIMES]   # todas
        recs = [memory_real.instance_to_records(i) for i in insts]
        row = {}
        for name, src in versions.items():
            sc = []
            for inst, rec in zip(insts, recs):
                g = memory_real.grade(evolve._run_candidate(src, rec, spec['func']), inst)
                sc.append((g['score'], g['fts'], g['queries']))
            nq = sum(q for _, _, q in sc) or 1
            row[name] = round(sum(s * q for s, _, q in sc) / nq, 4)
            row['orden_busqueda_actual(FTS)'] = round(sum(f * q for _, f, q in sc) / nq, 4)
            row['consultas'] = nq
        res['desarrollo' if split == 'dev' else 'reserva'] = row
    out['ndcg10'] = res
    return out


_BAD = ('def rank_memories(item):\n'
        '    return 1.0 - float(item.get("overlap", 0.0))\n')      # invierte el orden: claramente peor


def rollback_drill(store: Store) -> dict:
    """No toca la base real: usa una base temporal vacía (banco sintético) y el sandbox real."""
    from . import evolve
    tmp = os.path.join(tempfile.mkdtemp(prefix='vuppia_drill_'), 'drill.sqlite3')
    ts = Store(tmp)
    skill = 'memory_rank'
    steps = []
    good = evolve.register_version(ts, skill, evolve.REFERENCE[skill], origin='drill_good')
    with ts.tx() as db:
        db.execute("INSERT OR REPLACE INTO skill_pointers VALUES(?,'production',?,?)", (skill, good, utc()))
    evolve.record_release(ts, skill, 'production', 'promotion', None, good, 'drill', note='simulacro: versión buena')
    m0 = evolve.monitor_production(ts, skill)
    steps.append({'paso': 'monitoreo con versión buena', 'regresion': m0.get('regressed'),
                  'peor_regimen': m0.get('worst_regime_delta')})
    bad = evolve.register_version(ts, skill, _BAD, origin='drill_bad')
    with ts.tx() as db:
        db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='production'",
                   (bad, utc(), skill))
    evolve.record_release(ts, skill, 'production', 'promotion', good, bad, 'drill', note='simulacro: versión mala')
    m1 = evolve.monitor_production(ts, skill)
    now = ts.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))[0]
    steps.append({'paso': 'monitoreo con versión mala', 'regresion': m1.get('regressed'),
                  'peor_regimen': m1.get('worst_regime_delta'), 'revirtio_a': m1.get('rolled_back_to'),
                  'error': m1.get('error') or m1.get('rollback_error')})
    rel = ts.q("SELECT kind,decided_by,note FROM skill_releases WHERE skill=? ORDER BY id", (skill,))
    ok = (m0.get('regressed') is False and m1.get('regressed') is True and now == good)
    try:
        ts.db.close()
    except Exception:                                   # noqa: BLE001
        pass
    return {'paso_a_paso': steps, 'produccion_final_es_la_buena': now == good,
            'registro': [list(r) for r in rel], 'rollback_verificado': bool(ok)}
