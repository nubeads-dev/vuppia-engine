"""Runner autónomo 24/7 de VUPPIA SOFÍA, con guardas duras.

Corre un loop supervisado: cada 'tick' hace una unidad acotada de trabajo y vuelve a
dormir. Pensado para correr como servicio (systemd en un VPS) de forma desatendida.

GUARDAS QUE NO SE PUEDEN SALTEAR (por diseño, no por configuración):
- NUNCA promueve a producción (no tiene la clave Ed25519; la función ni se invoca acá).
- NUNCA toca vuppia.com ni ningún sitio ni archivo de producción.
- Respeta los topes de presupuesto en USD (diario/mensual/por-llamada) de providers.py.
  Si no hay claves o no hay saldo, corre SOLO trabajo gratis (experimentos en sandbox,
  observación de memoria, chequeos de integridad). La investigación con IA y la
  generación de código por IA solo ocurren con claves y dentro del presupuesto.
- Promueve candidatos de código SOLO al canal de LABORATORIO y solo si superan la
  evaluación oculta. La activación en producción sigue siendo manual y firmada.
- Freno: archivo VUPPIA_STOP (o señal) detiene el loop de forma limpia tras el tick actual.
- Cada acción queda en la bitácora encadenada. Hay heartbeat legible mientras corre.

Cada tick es transaccional e idempotente a nivel de registro: si el proceso se cae a
mitad, al reiniciar retoma sin corromper estado (la cola usa leases, la bitácora encadena).
"""
from __future__ import annotations

import os
import signal
import time
import traceback
from pathlib import Path

from . import agenda, evolve, memory, mission, objectives, planner
from .providers import BudgetExceeded, from_env
from .store import Store, utc


class _Stop:
    def __init__(self):
        self.flag = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass   # sin señales fuera del hilo principal

    def _handle(self, *_):
        self.flag = True


def _stop_requested(cfg) -> bool:
    return Path(cfg['stop_file']).exists()


def _budget_left(store: Store) -> float:
    from .providers import daily_budget
    spent = store.one("SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) FROM llm_calls "
                      "WHERE day=date('now')")[0]
    return daily_budget() - spent


def heartbeat(store: Store, state: dict) -> None:
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('autonomous_heartbeat',?)",
                   (utc() + ' | ' + ' '.join(f'{k}={v}' for k, v in state.items()),))


def _ensure_kv(store: Store):
    with store.tx() as db:
        db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')


def tick(store: Store, cfg: dict) -> dict:
    """Una unidad de trabajo autónomo acotada. Devuelve qué hizo. Nunca lanza hacia afuera."""
    did = {'at': utc(), 'actions': [], 'errors': [], 'cost_before': None, 'cost_after': None}
    cost0 = store.one("SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) FROM llm_calls "
                      "WHERE day=date('now')")[0]
    did['cost_before'] = round(cost0, 4)

    # 1) Integridad de la bitácora (gratis, siempre).
    try:
        ok = store.verify_ledger()['ok']
        did['actions'].append({'integrity': ok})
        if not ok:
            did['errors'].append('bitácora inconsistente: se frena el ciclo por seguridad')
            return did
    except Exception as exc:                          # noqa: BLE001
        did['errors'].append('integridad: ' + repr(exc)[:120]); return did

    # 2) Un experimento orientado por la Primera Ley (gratis, en sandbox).
    if cfg.get('experiments', True):
        try:
            plan = mission.plan_rule(store)
            mission.preregister(store, plan['id'])
            res = mission.execute(store, plan['id'], subject=from_env(store, 'subject'))
            did['actions'].append({'experiment': plan['id'], 'verdict': res['verdict']})
        except Exception as exc:                       # noqa: BLE001
            did['errors'].append('experimento: ' + repr(exc)[:120])

    # 3) Investigación externa + hipótesis por IA + evolución de código: SOLO con IA y presupuesto.
    proposer = from_env(store, 'proposer')
    if proposer and _budget_left(store) > 0.02:
        # 3a-bis) Si hay una investigación priorizada en la agenda y hay online, se busca sobre ESE tema.
        top = agenda.top_active(store)
        if top and os.getenv('VUPPIA_AUTONOMOUS_ONLINE') == '1' and _budget_left(store) > 0.05:
            try:
                from . import research
                r = research.investigate(store, top['question'] or top['topic'], online=True,
                                         researcher=proposer, verifier=from_env(store, 'verifier'), max_steps=2)
                did['actions'].append({'research': top['id'], 'refs': len(r['evidence'])})
            except BudgetExceeded:
                did['actions'].append({'research': 'presupuesto agotado'})
            except Exception as exc:                   # noqa: BLE001
                did['errors'].append('research: ' + repr(exc)[:120])
        # 3a) hipótesis nueva por IA (dentro de presupuesto).
        try:
            plan = mission.plan_ai(store, proposer)
            mission.preregister(store, plan['id'])
            res = mission.execute(store, plan['id'], subject=from_env(store, 'subject'))
            did['actions'].append({'ai_experiment': plan['id'], 'verdict': res['verdict']})
        except BudgetExceeded:
            did['actions'].append({'ai_experiment': 'presupuesto agotado'})
        except Exception as exc:                       # noqa: BLE001
            did['errors'].append('ai_experiment: ' + repr(exc)[:120])
        # 3b) un candidato de código evaluado y promovido SOLO a laboratorio.
        if _budget_left(store) > 0.02 and cfg.get('evolve', True):
            for skill in cfg.get('evolve_skills', ['hypothesis', 'memory_rank']):
                try:
                    src = evolve.generate_candidate(store, skill, proposer)
                    r = evolve.promote_lab(store, skill, src)
                    did['actions'].append({'evolve': skill, 'promoted_to_lab': r['promoted']})
                except BudgetExceeded:
                    did['actions'].append({'evolve': skill, 'result': 'presupuesto agotado'}); break
                except Exception as exc:               # noqa: BLE001
                    did['errors'].append(f'evolve {skill}: ' + repr(exc)[:120])

    # 4) Aprender de los resultados para decidir qué sigue (no repetir eternamente).
    try:
        did['actions'].append({'learn': _learn(store)})
    except Exception as exc:                           # noqa: BLE001
        did['errors'].append('learn: ' + repr(exc)[:120])

    cost1 = store.one("SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) FROM llm_calls "
                      "WHERE day=date('now')")[0]
    did['cost_after'] = round(cost1, 4)
    did['progress'] = objectives.progress(store)
    store.append('autonomous_tick', {'actions': len(did['actions']), 'errors': len(did['errors']),
                                     'cost_delta': round(cost1 - cost0, 4),
                                     'progress': did['progress']})
    try:
        heartbeat(store, {'at': did['at'], 'actions': len(did['actions']), 'errors': len(did['errors']),
                          'cost_today': did['cost_after'],
                          'experimentos': did['progress']['experimentos_corridos']})
    except Exception:                                  # noqa: BLE001
        pass
    return did


def _learn(store: Store) -> dict:
    """Usa los resultados recientes para reorganizar prioridades y detectar vacíos. No repite
    experimentos (el fingerprint ya impide duplicados); acá ajusta QUÉ investigar después.

    - Si una línea (protocolo) viene dando 'inconclusive'/'refuted' repetidamente, baja su
      prioridad en la agenda (o propone un vacío a cubrir).
    - Si hay un 'supported' nuevo, lo deja como progreso y propone un seguimiento.
    """
    rows = store.q("SELECT protocol,status,review FROM hypotheses WHERE status='analyzed' "
                   'ORDER BY id DESC LIMIT 12')
    if not rows:
        return {'note': 'sin resultados analizados todavía'}
    verdicts = {}
    for protocol, _status, review in rows:
        import json as _j
        try:
            v = _j.loads(review).get('verdict') if review else None
        except (ValueError, TypeError):
            v = None
        verdicts.setdefault(protocol, []).append(v)
    actions = []
    for protocol, vs in verdicts.items():
        recent = vs[:5]
        if len(recent) >= 4 and all(v in ('inconclusive', 'invalid', 'refuted') for v in recent):
            # línea agotada: proponer un vacío a cubrir en vez de insistir
            exists = store.one("SELECT 1 FROM agenda WHERE topic LIKE ? AND status='proposed'",
                               (f'%{protocol}%',))
            if not exists:
                agenda.add(store, f'Revisar el enfoque de {protocol} (viene sin efecto)',
                           f'El protocolo {protocol} no da efecto en varias corridas: ¿cambiar el '
                           'control, el sustrato o la métrica? Buscar alternativas.',
                           status='proposed', origin='sofia_learn',
                           note='detectado por aprendizaje autónomo')
                actions.append(f'propuse revisar {protocol} (línea sin efecto)')
        elif 'supported' in recent[:2]:
            actions.append(f'{protocol}: efecto sostenido, mantengo la línea')
    return {'ajustes': actions or ['sin cambios de prioridad'], 'lineas': {k: v[:3] for k, v in verdicts.items()}}


def run_forever(db_path, *, interval_s=900, max_iterations=0, stop_file='VUPPIA_STOP',
                experiments=True, evolve_flag=True) -> dict:
    """Loop supervisado. interval_s entre ticks; max_iterations=0 → infinito (hasta freno).
    Se detiene limpio ante VUPPIA_STOP, SIGTERM/SIGINT o presupuesto diario agotado si así se
    configura. Cada tick está aislado: una excepción no tumba el loop."""
    cfg = {'stop_file': stop_file, 'experiments': experiments, 'evolve': evolve_flag,
           'evolve_skills': ['hypothesis', 'memory_rank']}
    stop = _Stop()
    store = Store(db_path)
    _ensure_kv(store)
    iterations = 0
    summary = {'started_at': utc(), 'ticks': 0, 'total_errors': 0}
    while True:
        if stop.flag or _stop_requested(cfg):
            summary['stopped_reason'] = 'freno solicitado'; break
        if max_iterations and iterations >= max_iterations:
            summary['stopped_reason'] = 'max_iterations'; break
        iterations += 1
        try:
            result = tick(store, cfg)
            summary['ticks'] += 1
            summary['total_errors'] += len(result['errors'])
            heartbeat(store, {'tick': iterations, 'actions': len(result['actions']),
                              'errors': len(result['errors']), 'cost_today': result['cost_after']})
        except Exception:                              # noqa: BLE001 - el loop nunca muere por un tick
            summary['total_errors'] += 1
            store.append('autonomous_error', {'trace': traceback.format_exc()[-400:]})
        # dormir en tramos cortos para responder rápido al freno
        slept = 0.0
        while slept < interval_s and not stop.flag and not _stop_requested(cfg):
            time.sleep(min(2.0, interval_s - slept)); slept += 2.0
    summary['ended_at'] = utc()
    store.append('autonomous_stop', summary)
    store.db.close()
    return summary
