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


def _chat_active(store: Store) -> bool:
    """True si alguien está chateando ahora: el motor cede la cuota del modelo al chat."""
    try:
        row = store.one("SELECT v FROM kv WHERE k='chat_active_until'")
        if not row or not row[0]:
            return False
        from datetime import datetime as _dt, timezone as _tz
        return _dt.fromisoformat(row[0]) > _dt.now(_tz.utc)
    except Exception:                                   # noqa: BLE001
        return False


def _low_quota(store: Store, floor: int = 4000, model: str | None = None) -> bool:
    """True si la cuota de tokens/min del proveedor está por debajo del piso: el motor
    frena sus llamadas al modelo para dejarle margen al chat (tier gratis tiene poco)."""
    try:
        import json as _j
        row = store.one("SELECT v FROM kv WHERE k='provider_quota'")
        if not row or not row[0]:
            return False
        q = _j.loads(row[0])
        rem = q.get('remaining_tokens')
        if rem is None:
            return False
        # La cuota es POR MODELO y por minuto: una lectura de otro modelo (p. ej. el del chat)
        # o de hace más de 90 s no dice nada de la cuota actual del motor. Sin esto, una lectura
        # baja y vieja dejaba al motor frenado para siempre (nadie volvía a llamar y a refrescarla).
        if model and q.get('model') and q.get('model') != model:
            return False
        try:
            from datetime import datetime as _dt, timezone as _tz
            if (_dt.now(_tz.utc) - _dt.fromisoformat(q.get('at'))).total_seconds() > 90:
                return False
        except Exception:                               # noqa: BLE001
            pass
        return int(float(rem)) < floor
    except Exception:                                   # noqa: BLE001
        return False


def _budget_left(store: Store) -> float:
    from .providers import daily_budget
    spent = store.one("SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) FROM llm_calls "
                      "WHERE day=date('now')")[0]
    return daily_budget() - spent


def _improve_from_research(store: Store, proposer, research_result: dict, topic: str):
    """Puente literatura → auto-mejora: a partir de lo que ACABA de leer, formula UNA
    hipótesis concreta y falsable sobre cómo mejorar su propia arquitectura (memoria,
    metacognición, incertidumbre, aprendizaje continuo, autorrepresentación) y la encola.
    No inventa resultados: se apoya en la síntesis/evidencia del research que corrió."""
    syn = (research_result or {}).get('synthesis') or {}
    findings = (syn.get('answer') or '')[:1200]
    ev = (research_result or {}).get('evidence') or []
    if not findings and not ev:
        return None
    srcs = '; '.join((s.get('title') or '')[:80] for s in ev[:4])
    system = (
        'Sos VUPPIA SOFÍA, un sistema de investigación de IA que se automejora. Leíste literatura '
        'científica. A partir de ESO, proponé UNA mejora concreta y falsable de tu PROPIA arquitectura '
        '(elegí un eje: memoria, metacognición/estimación de incertidumbre, aprendizaje continuo, '
        'razonamiento o autorrepresentación). Debe poder probarse como experimento con un control y una '
        'métrica. No afirmes ser consciente. No inventes resultados. Respondé SOLO JSON: '
        '{"pregunta": "<pregunta de investigación falsable, 1 línea>", "mecanismo": "<qué mecanismo de la '
        'literatura la inspira, 1 línea>"}.')
    user = f'Tema investigado: {topic}\nHallazgos: {findings}\nFuentes: {srcs}'
    try:
        raw = proposer.complete(system, user, purpose='improve_from_research', max_tokens=220, temperature=0.6)
        from .providers import extract_json
        obj = extract_json(raw)
        q = (obj.get('pregunta') or '').strip()
        mech = (obj.get('mecanismo') or '').strip()
        if not q or len(q) < 10:
            return None
        aid = agenda.add(store, q[:80], q + (f' [inspirado en: {mech}]' if mech else ''),
                         status='active', origin='sofia', priority=2.0)
        return {'id': aid, 'question': q, 'mechanism': mech}
    except Exception:                                   # noqa: BLE001
        return None


def _self_question(store: Store, proposer):
    """Curiosidad autónoma: el cerebro inventa UNA pregunta de investigación nueva, falsable,
    a partir de lo que ya midió, y la agenda como tema activo. Así VUPPIA se pregunta cosas sola."""
    rows = store.q("SELECT h.question, r.verdict FROM results r JOIN preregistrations p ON r.prereg_id=p.id "
                   "JOIN hypotheses h ON p.hypothesis_id=h.id ORDER BY r.id DESC LIMIT 8")
    contexto = '; '.join(f'{q} -> {v}' for q, v in rows) or 'sin resultados aún'
    existentes = store.q("SELECT topic FROM agenda ORDER BY id DESC LIMIT 15")
    evitar = '; '.join(t[0] for t in existentes) or '(ninguna)'
    system = ('Sos VUPPIA, una IA que investiga sus propias capacidades FUNCIONALES (no conciencia). '
              'Generá UNA sola pregunta de investigación NUEVA, falsable, concreta y medible, distinta de '
              'lo ya hecho. Sobre redes neuronales, calibración, incertidumbre, generalización, '
              'metacognición funcional. Respondé SOLO la pregunta, una línea, sin explicación.')
    user = ('Resultados recientes: ' + contexto + '\nTemas que YA tengo (no repitas): ' + evitar +
            '\nPregunta nueva:')
    try:
        q = proposer.complete(system, user, purpose='self_question', max_tokens=120, temperature=0.9)
    except Exception:
        return None
    q = (q or '').strip().strip('"').split('\n')[0][:200]
    if len(q) < 8:
        return None
    from . import agenda
    aid = agenda.add(store, q, q, status='active', origin='sofia', priority=1.5)
    return {'id': aid, 'question': q}


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

    # 0) Carta fundacional (misión): persistente y auditable; no-op si no cambió.
    try:
        from . import charter
        charter.record(store)
    except Exception:                                   # noqa: BLE001
        pass

    # 1) Integridad de la bitácora (gratis, siempre).
    try:
        ok = store.verify_ledger()['ok']
        did['actions'].append({'integrity': ok})
        if not ok:
            did['errors'].append('bitácora inconsistente: se frena el ciclo por seguridad')
            return did
    except Exception as exc:                          # noqa: BLE001
        did['errors'].append('integridad: ' + repr(exc)[:120]); return did

    # 2) Un experimento ELEGIDO POR LA POLÍTICA (valor de información + incertidumbre, con
    #    la skill evolucionada y regret registrado). Si falla, cae a la rotación por cobertura.
    if cfg.get('experiments', True):
        try:
            try:
                plan = mission.plan_policy(store)
                did['actions'].append({'seleccion': 'politica', 'regret': plan.get('planner', {}).get('regret')})
            except Exception as exc:                       # noqa: BLE001
                plan = mission.plan_rule(store)
                did['actions'].append({'seleccion': 'rotacion', 'motivo': repr(exc)[:80]})
            mission.preregister(store, plan['id'])
            res = mission.execute(store, plan['id'], subject=from_env(store, 'subject'))
            did['actions'].append({'experiment': plan['id'], 'verdict': res['verdict']})
        except Exception as exc:                       # noqa: BLE001
            did['errors'].append('experimento: ' + repr(exc)[:120])

    # 3) Investigación externa + hipótesis por IA + evolución de código: con IA y presupuesto,
    # o con un proveedor GRATUITO (costo real $0: no consume presupuesto, así que no se bloquea en 0).
    proposer = from_env(store, 'proposer')
    _free = bool(proposer) and getattr(proposer, 'name', '') in ('groq', 'openrouter', 'gemini', 'cerebras', 'pool')
    # Conteo histórico de ticks (gatea los pasos periódicos). Se calcula SIEMPRE, afuera del
    # freno por cuota, para que los pasos SIN LLM (auto-aplicación y monitoreo de producción,
    # puro sandbox/CPU) no queden rehenes de la cuota del modelo.
    _ntick = store.one("SELECT COUNT(*) FROM ledger WHERE kind='autonomous_tick'")[0]
    if proposer and _chat_active(store):
        # Alguien está chateando: le cedemos la cuota del modelo este tick (el experimento
        # por regla ya corrió arriba, así que el motor igual avanza sin tocar el LLM).
        did['actions'].append({'chat_priority': 'cedí la cuota del modelo al chat'})
    elif proposer and (not proposer.has_capacity() if getattr(proposer, 'name', '') == 'pool'
                       else _low_quota(store, model=getattr(proposer, 'model', None))):
        # Queda poca cuota por minuto del proveedor: el motor sipea y deja aire para el chat.
        did['actions'].append({'quota_brake': 'poca cuota del modelo; salteo pasos de LLM este tick'})
    elif proposer and (_free or _budget_left(store) > 0.02):
        # 3a-bis) Investigación de un tema de la agenda: pesada (web + LLM), cada 3 ticks.
        top = agenda.top_active(store)
        if top and os.getenv('VUPPIA_AUTONOMOUS_ONLINE') == '1' and _ntick % 3 == 0 and (_free or _budget_left(store) > 0.05):
            try:
                from . import research
                _topic = top['question'] or top['topic']
                r = research.investigate(store, _topic, online=True,
                                         researcher=proposer, verifier=from_env(store, 'verifier'), max_steps=2)
                did['actions'].append({'research': top['id'], 'refs': len(r['evidence'])})
                # Puente: convierte lo leído en una hipótesis de auto-mejora y la encola.
                imp = _improve_from_research(store, proposer, r, _topic)
                if imp:
                    did['actions'].append({'improve_from_research': imp['id'], 'q': imp['question'][:80]})
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
        # 3b) un candidato de código evaluado y promovido SOLO a laboratorio: pesado (2x4000 tokens), cada 4 ticks.
        if (_free or _budget_left(store) > 0.02) and cfg.get('evolve', True) and _ntick % 4 == 0:
            for skill in cfg.get('evolve_skills', ['hypothesis', 'memory_rank']):
                promoted = False
                try:
                    src = evolve.generate_candidate(store, skill, proposer)
                    r = evolve.promote_lab(store, skill, src)
                    promoted = bool(r.get('promoted'))
                    did['actions'].append({'evolve': skill, 'promoted_to_lab': promoted})
                except BudgetExceeded:
                    did['actions'].append({'evolve': skill, 'result': 'presupuesto agotado'}); break
                except Exception as exc:               # noqa: BLE001
                    did['errors'].append(f'evolve {skill}: ' + repr(exc)[:120])
                # Respaldo: si el LLM no mejoró, probar la mejora de referencia curada.
                # El juez independiente la evalúa igual; si no mejora, la rechaza.
                if not promoted:
                    try:
                        ref = evolve.try_reference(store, skill)
                        if ref.get('status') == 'promovido':
                            did['actions'].append({'evolve_reference': skill, 'promoted_to_lab': True})
                    except Exception as exc:           # noqa: BLE001
                        did['errors'].append(f'evolve_ref {skill}: ' + repr(exc)[:120])
        # (El monitoreo de producción y la auto-aplicación se movieron AFUERA del freno por
        #  cuota, más abajo: no usan LLM y no deben depender de la cuota del modelo.)
        # 3b-bis) Estudio sobre el MODELO REAL (protocolos LLM): caro (muchas llamadas), así que
        # ocasional — 1 de cada 8 ticks. Así VUPPIA se estudia a sí misma sin trabar el motor.
        _subj = from_env(store, 'subject')
        if _subj:
            try:
                if _ntick % 12 == 0:
                    lplan = mission.plan_rule(store, kinds=('llm',))
                    mission.preregister(store, lplan['id'])
                    lres = mission.execute(store, lplan['id'], subject=_subj)
                    did['actions'].append({'llm_self_study': lplan['id'], 'verdict': lres['verdict']})
            except Exception as exc:                       # noqa: BLE001
                did['errors'].append('llm_self_study: ' + repr(exc)[:120])
        # 3c) Curiosidad autónoma: si hay pocos temas activos, se hace UNA pregunta nueva y la agenda.
        try:
            n_active = store.one("SELECT COUNT(*) FROM agenda WHERE status='active'")[0]
            if n_active < 3:
                sq = _self_question(store, proposer)
                if sq:
                    did['actions'].append({'self_question': sq['id'], 'q': sq['question'][:80]})
        except Exception as exc:                           # noqa: BLE001
            did['errors'].append('self_question: ' + repr(exc)[:120])
        # 3e) Blog SEO: 1 artículo cada 6 ticks (es pesado ~2200 tok y nada urgente; así
        # deja cuota por minuto para el chat y el resto del motor). Cubre el backlog igual.
        if _ntick % 6 == 0:
            try:
                from . import blog_gen
                art = blog_gen.generate_one(store, proposer)
                if art:
                    did['actions'].append({'article': art['result_id'], 'slug': art['slug']})
            except Exception as exc:                       # noqa: BLE001
                did['errors'].append('blog: ' + repr(exc)[:120])

    # 3f) Pasos SIN LLM (puro sandbox/CPU): corren SIEMPRE, afuera del freno por cuota del
    # modelo, porque no consumen cuota de Groq. Así el loop de autoprogramación (evaluar una
    # mejora ya existente en lab y aplicarla) no queda bloqueado cuando el proveedor está topado.
    # Monitoreo de PRODUCCIÓN + rollback automático (VUPPIA_AUTO_ROLLBACK=1), cada 8 ticks.
    if os.getenv('VUPPIA_AUTO_ROLLBACK') == '1' and _ntick % 8 == 0:
        for skill in cfg.get('evolve_skills', ['hypothesis', 'memory_rank']):
            try:
                mon = evolve.monitor_production(store, skill)
                if mon.get('regressed'):
                    did['actions'].append({'auto_rollback': skill, 'to': mon.get('rolled_back_to')})
            except Exception as exc:                       # noqa: BLE001
                did['errors'].append(f'monitor {skill}: ' + repr(exc)[:120])
    # AUTO-APLICACIÓN de bajo riesgo (VUPPIA_AUTOAPPLY=1, autorización previa del operador): si
    # una mejora de scoring verificada está en lab por delante de producción, la aplica SOLA.
    # Lo crítico/arquitectura NUNCA pasa por acá: eso siempre exige firma.
    # CONGELADO (23/11): ninguna incorporación automática a producción hasta que existan las
    # pruebas independientes y la autorización del operador. Se habilita creando el archivo
    # AUTOAPPLY_OK en el volumen de datos; la variable de entorno sola ya no alcanza.
    from . import providers as _pv
    _autoapply_ok = os.path.exists(os.path.join(_pv.data_dir(), 'AUTOAPPLY_OK'))
    if _autoapply_ok and os.getenv('VUPPIA_AUTOAPPLY') == '1' and _ntick % 4 == 0:
        for skill in cfg.get('evolve_skills', ['hypothesis', 'memory_rank']):
            try:
                ap = evolve.auto_apply(store, skill)
                if ap.get('status') == 'aplicado':
                    did['actions'].append({'auto_apply': skill, 'to': (ap.get('promoted_to_production') or '')[:12]})
            except Exception as exc:                       # noqa: BLE001
                did['errors'].append(f'autoapply {skill}: ' + repr(exc)[:120])

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
    # Observabilidad: guardar el DETALLE del último tick en kv (legible por admin), porque los
    # prints del worker quedan en el buffer y no llegan a los logs. Así se ve qué hizo cada tick.
    try:
        import json as _json
        with store.tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')
            db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('last_tick',?)",
                       (_json.dumps({'at': did.get('at'), 'ntick': _ntick,
                                     'actions': did['actions'][-14:], 'errors': did['errors'][-8:]})[:6000],))
    except Exception:                                  # noqa: BLE001
        pass
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
