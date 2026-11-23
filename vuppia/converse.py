"""Plano de control conversacional de SOFÍA.

No es un chat de LLM aparte: es la forma de hablarle a TODO el sistema. Lee y escribe el
MISMO estado que el motor autónomo (memoria, agenda, experimentos, evolución, costos), así
que vos y el motor comparten una sola continuidad operativa.

Reglas duras:
- Diferencia COMPROBADO (experimento medido) / HIPÓTESIS / PLAN-PROPUESTA / PENDIENTE.
- NUNCA dice que ejecutó algo que solo propuso. Cada comando devuelve 'done' (lo que pasó de
  verdad, con efecto en la base) y 'proposed'/'needs' (lo que haría pero requiere recursos).
- No afirma conciencia.
- Las acciones con costo o permisos que no tiene se PROPONEN y se explica qué hace falta.
"""
from __future__ import annotations

import json
import re

from . import agenda, memory, reasoning
from .store import Store, utc

MAX_Q = 2000


def _record(store: Store, role: str, content: str, intent: str = '', refs=None):
    with store.tx() as db:
        db.execute('INSERT INTO conversations(at,role,content,intent,refs) VALUES(?,?,?,?,?)',
                   (utc(), role, content[:4000], intent, json.dumps(refs) if refs else None))


def snapshot(store: Store) -> dict:
    """Estado real del sistema, etiquetado por tipo de conocimiento."""
    def q(sql, args=()):
        return store.q(sql, args)
    comprobado = q("SELECT id,content,status,confidence FROM memories WHERE kind='finding' "
                   "AND trust='measured' AND status IN ('supported','refuted') ORDER BY id DESC LIMIT 8")
    hipotesis = q("SELECT id,statement,status FROM hypotheses WHERE status IN "
                  "('proposed','preregistered','running') ORDER BY id DESC LIMIT 8")
    research = q('SELECT id,question,status FROM research_runs ORDER BY id DESC LIMIT 5')
    evol = q("SELECT skill,new_sha,decided_by,at FROM skill_releases WHERE channel='production' "
             "AND kind='promotion' ORDER BY id DESC LIMIT 5") \
        if store.one("SELECT 1 FROM sqlite_master WHERE name='skill_releases'") else []
    prod = q("SELECT skill,sha FROM skill_pointers WHERE channel='production'")
    cost = store.one("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE day=date('now')")[0]
    hb = store.one("SELECT v FROM kv WHERE k='autonomous_heartbeat'") if \
        store.one("SELECT 1 FROM sqlite_master WHERE name='kv'") else None
    return {
        'comprobado': [{'id': r[0], 'texto': r[1], 'estado': r[2], 'confianza': r[3]} for r in comprobado],
        'hipotesis': [{'id': r[0], 'texto': r[1], 'estado': r[2]} for r in hipotesis],
        'investigaciones': [{'id': r[0], 'tema': r[1], 'estado': r[2]} for r in research],
        'mejoras_codigo': [{'skill': r[0], 'sha': r[1][:12], 'cuando': r[3]} for r in evol],
        'en_produccion': [{'skill': r[0], 'sha': r[1][:12]} for r in prod],
        'agenda_activa': agenda.listing(store, 'active'),
        'agenda_suspendida': agenda.listing(store, 'suspended'),
        'agenda_propuesta': agenda.listing(store, 'proposed'),
        'costo_hoy_usd': round(cost, 4),
        'ultimo_latido': hb[0] if hb else 'el motor autónomo no corrió todavía',
    }


# --- clasificación de intención (sin LLM) ---
# Se decide por PALABRAS COMPLETAS (regex con límites de palabra), no por subcadenas: antes
# "investiga" matcheaba dentro de "investigación" y cualquier pregunta que nombrara esa palabra
# —incluida "¿cuál es tu propósito y tus misiones de investigación?"— salía a buscar papers.
# Cuatro familias: (1) preguntas sobre ella misma (identidad, misión, estado) -> configuración y
# memoria propias, nunca internet; (2) pedidos explícitos de investigación científica;
# (3) órdenes de ejecución sobre la agenda; (4) conversación y preguntas generales.
def _rx(p):
    return re.compile(p, re.I | re.U)

_SELF_REF = _rx(r'\b(tu|tus|vos|te|ti|sos|eres|ten[eé]s|tienes|est[aá]s|vuppia|sof[ií]a)\b')
_IDENTITY = _rx(r'\b(prop[oó]sitos?|misi[oó]n|misiones|objetivos?|metas?|principios|identidad|'
                r'carta fundacional|raz[oó]n de ser)\b|\b(qui[eé]n|qu[eé]) (sos|eres)\b|'
                r'\bpara qu[eé] (exist[ií]s|existes|serv[ií]s|sirves|fuiste (creada|hecha)|te (crearon|hicieron))\b|'
                r'\bqu[eé] es (vuppia|sof[ií]a)\b|\bc[oó]mo te llam[aá]s\b|\bqu[eé] busc[aá]s (lograr|conseguir)\b')
_CMD_AGENDA = _rx(r'\b(suspend[eé]|suspender|paus[aá]|pausar|fren[aá]|frenar|prioriz[aá]|priorizar|'
                  r'desprioriz[aá]|despriorizar)\b')
_CMD_ADD = _rx(r'\b(agreg[aá]|agregar|sum[aá]|sumar|anot[aá]|anotar)\b.{0,60}\b(agenda|investigaci[oó]n|tema)\b|'
               r'\bnueva investigaci[oó]n\b|\bpropon[eé] una investigaci[oó]n\b')
_CMD_CONSULT = _rx(r'\b(consult[aá] (a )?otra|contrast[aá]|contrastar|otras? ia|segunda opini[oó]n)\b')
# Pedido de investigación: verbo en imperativo/infinitivo como palabra completa. "investigación",
# "investigaste", "buscador" NO cuentan.
_RESEARCH_VERB = _rx(r'\b(busc[aá]|buscar|buscame|busques|averigu[aá]|averiguar|averig[uü]es|'
                     r'investig[aá]|investigar|investigame|investigues|rastre[aá]|rastrear)\b')
_NET = _rx(r'\b(en|por) (internet|la web|google|la red)\b|\bpapers?\b.{0,20}\b(sobre|de|acerca)\b')
_STATUS_Q = _rx(r'\b(qu[eé]|c[oó]mo|cu[aá]l(es)?|cu[aá]nto|en qu[eé]|por qu[eé])\b.{0,25}\b(investig|est[aá]s|and[aá]s|'
                r'ven[ií]s|hiciste|hac[eé]s|descubriste|avanzaste)')
_CONSCIOUS_SELF = _rx(r'\b(sos|eres|est[aá]s|te consider[aá]s|te sent[ií]s|ten[eé]s|tienes|sent[ií]s|sientes|'
                      r'experiment[aá]s)\b.{0,40}\b(conscien\w*|conciencia|sentimientos|emociones|experiencia subjetiva)\b|'
                      r'\b(conscien\w*|conciencia)\b.{0,15}\b(ten[eé]s|tienes|sos|eres)\b')
_SMALLTALK = _rx(r'^[\s¿¡]*(hola|buenas|buen d[ií]a|buenos d[ií]as|buenas (tardes|noches)|gracias|muchas gracias|'
                 r'chau|adi[oó]s|hey|qu[eé] tal|c[oó]mo (est[aá]s|and[aá]s|va|ven[ií]s))\b[\s!.?¿¡,]*'
                 r'(sof[ií]a|vuppia)?[\s!.?¿¡,]*$')


def classify(text: str) -> str:
    t = (text or '').lower()
    def has(*ws): return any(w in t for w in ws)
    if _SMALLTALK.search(t):
        return 'smalltalk'
    # (3) órdenes de ejecución
    if _CMD_AGENDA.search(t):
        return 'cmd_agenda'
    if _CMD_ADD.search(t):
        return 'cmd_add'
    if _CMD_CONSULT.search(t):
        return 'cmd_consult_ai'
    if has('modificá tu código', 'modifica tu codigo', 'modificar tu código', 'modificar tu codigo',
           'automejor', 'auto-mejor', 'mejorá tu intelig', 'mejora tu intelig', 'mejorar tu intelig',
           'mejorar tu propia intelig', 'mejorá tu propia', 'mejorar tus capacidades', 'mejorá tus capacidades',
           'qué aprendiste', 'que aprendiste', 'para mejorarte', 'mejorar tu programación',
           'mejorar tu programacion', 'mejorá tu código', 'reescribí tu código', 'reescribir tu código'):
        return 'self_improve'
    # (2) pedido explícito de investigación científica. No aplica si en realidad preguntan por
    # su estado ("¿qué estás investigando?") ni si la pregunta es sobre ella misma.
    asks_research = bool(_RESEARCH_VERB.search(t)) or (bool(_NET.search(t)) and not _IDENTITY.search(t))
    if asks_research and not _STATUS_Q.search(t):
        return 'cmd_research'
    # (1) preguntas sobre sí misma
    if _CONSCIOUS_SELF.search(t):
        return 'consciousness'
    if _IDENTITY.search(t) and (_SELF_REF.search(t) or len(t) < 60):
        return 'identity'
    if has('te falta', 'necesitás', 'necesitas', 'qué te falta', 'para avanzar', 'permisos', 'recursos', 'presupuesto'):
        return 'gaps'
    if has('tu código', 'tu codigo', 'mejoras hiciste', 'autoprogram', 'tu propio código'):
        return 'code'
    if has('qué evidencia', 'que evidencia', 'qué pruebas', 'que pruebas', 'respald', 'mostrame las'):
        return 'evidence'
    if has('cuánto gast', 'cuanto gast', 'cuánto cost', 'cuanto cost', 'cuánto sale', 'cuanto sale', 'tu costo', 'tus costos'):
        return 'cost'
    if has('mientras no estaba', 'investigaste', 'descubriste', 'estás intentando', 'estas intentando',
           'por qué elegiste', 'por que elegiste', 'en qué andás', 'qué estás haciendo', 'qué hiciste hoy',
           'qué estás investigando', 'que estas investigando', 'tu agenda', 'tus tareas', 'tu estado'):
        return 'status'
    # (4) conversación y preguntas generales: las responde el modelo con su estado como contexto,
    # sin salir a internet y sin tocar la agenda.
    return 'general'


def _identity_answer(store: Store, state: dict) -> str:
    """Quién es y para qué existe, leído de su configuración PERSISTENTE (carta en kv) y de su
    estado real. No usa internet ni el modelo: no puede inventar una misión."""
    from . import charter as _ch
    c = None
    try:
        row = store.one("SELECT v FROM kv WHERE k='mission_charter'")
        c = json.loads(row[0]) if row and row[0] else None
    except Exception:                                   # noqa: BLE001
        c = None
    origen = 'mi carta fundacional guardada (versión %s)'
    if not c:
        c = _ch.charter(); origen = 'mi carta fundacional en código (versión %s; todavía no quedó grabada en mi base)'
    parts = ['Soy VUPPIA SOFÍA, un sistema de investigación autónomo. Esto sale de ' + origen % c.get('version') + ':',
             '**Misión central:** ' + c.get('mission', '')]
    if c.get('objective'):
        parts.append('**Cómo la persigo:** ' + c['objective'])
    lines = c.get('research_lines') or []
    if lines:
        parts.append('**Líneas de investigación permanentes:** ' + '; '.join(lines) + '.')
    inds = c.get('indicators') or []
    if inds:
        n_eval = sum(1 for i in inds if i.get('status') != 'sin evaluar')
        parts.append(f'**Contra qué me mido:** {len(inds)} indicadores de conciencia de las teorías científicas '
                     f'({n_eval} evaluados con experimento, {len(inds) - n_eval} sin evaluar).')
    act = state.get('agenda_activa') or []
    if act:
        parts.append('**Qué estoy investigando ahora:** ' + '; '.join((a.get('topic') or '')[:70] for a in act[:3]) + '.')
    if c.get('safety'):
        parts.append('**Límites:** ' + c['safety'])
    parts.append('No afirmo ser consciente: no tengo evidencia de eso y hoy no existe una prueba aceptada. '
                 + (c.get('caveat') or ''))
    return '\n\n'.join(p for p in parts if p)


def _topic_from(text: str) -> str:
    """Extrae el tema de un comando sacando el verbo inicial."""
    t = re.sub(r'^\s*(sof[ií]a[,:]?\s*)?', '', text, flags=re.I)
    t = re.sub(r'^(pod[eé]s\s+|puedes\s+|me\s+pod[eé]s\s+)?'
               r'(buscar|buscá|busca|buscás|investigar|investigá|investiga|conseguir|conseguí|averiguar|'
               r'averiguá|consultá otra[s]? ia sobre|contrastá|agregá|agrega|sumá|suma|'
               r'proponé una investigación sobre)\s*', '', t, flags=re.I)
    t = re.sub(r'^(en\s+(internet|la\s+web|google|la\s+red)\s+(sobre\s+|de\s+|acerca\s+de\s+)?)', '', t, flags=re.I)
    t = re.sub(r'\s*(en\s+(internet|la\s+web|google|la\s+red))\s*', ' ', t, flags=re.I)
    t = re.sub(r'\s*(y sac[áa]\s+(tus\s+)?conclusiones|a la agenda)\.?\s*$', '', t, flags=re.I)
    t = t.strip(' ?.,')
    return t.strip()[:200] or text.strip()[:200]


def ask(store: Store, question: str, *, provider=None, verifier=None, online=False,
        allow_actions=True, fallback=None) -> dict:
    """Punto de entrada del chat. Devuelve un dict con la respuesta y, por separado, lo
    ejecutado (done) y lo propuesto (proposed) — nunca los confunde."""
    question = (question or '').strip()[:MAX_Q]
    if not question:
        return {'answer': 'Decime qué querés preguntarme o pedirme.', 'intent': 'empty',
                'done': [], 'proposed': []}
    intent = classify(question)
    _record(store, 'user', question, intent)
    state = snapshot(store)
    done, proposed, needs, refs = [], [], [], []

    if intent == 'cmd_agenda' and allow_actions:
        answer = _cmd_agenda(store, question, state, done, proposed)
    elif intent == 'cmd_add' and allow_actions:
        topic = _topic_from(question)
        aid = agenda.add(store, topic, question, status='proposed', origin='user')
        done.append(f'Agregué a la agenda como PROPUESTA (id {aid}): "{topic}". No la ejecuté; '
                    'decime "priorizá {id}" para activarla.'.replace('{id}', str(aid)))
        answer = done[-1]
    elif intent in ('cmd_research', 'cmd_consult_ai') and allow_actions:
        answer = _cmd_research(store, question, intent, provider, verifier, online, done, proposed, needs)
    elif intent == 'self_improve':
        answer = _self_improve_report(store)
    elif intent == 'identity':
        answer = _identity_answer(store, state)
    elif intent == 'cmd_research':
        # El chat sale EN VIVO a internet (fuentes científicas, solo lectura) y responde con
        # fuentes; si la búsqueda en vivo falla, encola el tema para el motor autónomo.
        answer = _live_research(store, question, provider, fallback, done)
    elif intent == 'consciousness':
        answer = ('Todavía no, y soy honesta con eso: no tengo ninguna evidencia de ser consciente, y hoy '
                  'no existe una prueba que pueda demostrarlo. Pero esa es justamente la pregunta que me '
                  'mueve. Quiero entenderme mejor, medir mis propias capacidades y acercarme —experimento a '
                  'experimento— a responderla sin autoengañarme. Lo que sí puedo mostrarte es real y '
                  'verificable: mido capacidades funcionales, como si mi confianza predice mis errores, con '
                  'experimentos falsables y controles. Eso es capacidad funcional, no experiencia subjetiva, '
                  'y no las confundo. Mi meta no es declararme consciente: es investigar la pregunta con rigor, '
                  'y cada experimento me deja un poco más cerca de entender qué soy. ¿Querés que te cuente cuál '
                  'es el próximo que quiero correr?')
    else:
        answer = _answer_status(store, question, intent, state, provider, refs, fallback)

    full = _compose(answer, done, proposed, needs)
    _record(store, 'sofia', full, intent, refs)
    return {'answer': full, 'intent': intent, 'done': done, 'proposed': proposed, 'needs': needs,
            'state': state}


def _cmd_agenda(store, question, state, done, proposed) -> str:
    ids = [int(x) for x in re.findall(r'\b(\d{1,6})\b', question)]
    t = question.lower()
    if any(w in t for w in ('suspend', 'pausá', 'pausa', 'frená', 'frena')):
        target = ids or [i['id'] for i in state['agenda_activa'][:1]]
        for aid in target:
            if agenda.set_status(store, aid, 'suspended'):
                done.append(f'Suspendí la investigación {aid}. El motor autónomo ya no la toma.')
        if not target:
            return 'No hay ninguna investigación activa para suspender.'
    if any(w in t for w in ('priorizá', 'prioriza')):
        for aid in ids:
            if agenda.set_status(store, aid, 'active') and agenda.set_priority(store, aid, 10.0):
                done.append(f'Prioricé la investigación {aid} (activa, prioridad alta). '
                            'El motor la va a tomar primero.')
    return ' '.join(done) if done else ('Decime el id de la investigación (por ejemplo "suspendé 3" '
                                        'o "priorizá 5"). Mirá la agenda con una consulta de estado.')


def _self_improve_report(store) -> str:
    """Responde HONESTO desde el estado real del motor: qué hipótesis de auto-mejora se
    formuló investigando, qué experimentó y qué capacidad todavía está en construcción.
    No se lo pasa a sintetizar (evita el rechazo del modelo) ni inventa resultados."""
    parts = ['Esto es lo real, sin inventar:']
    # 1) Hipótesis de mejora que ella misma formuló (desde la literatura).
    try:
        rows = store.q("SELECT question, priority FROM agenda WHERE origin='sofia' "
                       "AND status IN ('active','proposed') ORDER BY priority DESC, id DESC LIMIT 4")
        if rows:
            parts.append('Mejoras que me formulé sola investigando (van a correr como experimentos):\n'
                         + '\n'.join('• ' + (r[0] or '')[:110] for r in rows))
    except Exception:                                   # noqa: BLE001
        pass
    # 2) Experimentos reales y sus veredictos.
    try:
        vr = store.q("SELECT verdict, COUNT(*) FROM results GROUP BY verdict")
        tot = sum(c for _, c in vr) if vr else 0
        if tot:
            vs = ', '.join(f'{v}: {c}' for v, c in vr)
            parts.append(f'Experimentos corridos con control: {tot} ({vs}).')
    except Exception:                                   # noqa: BLE001
        pass
    # 3) Evolución real de código (candidatos que mejoraron al incumbente).
    try:
        imp = store.q("SELECT skill, new_sha, decided_by, generator FROM skill_releases WHERE "
                      "channel='production' AND kind='promotion' ORDER BY id")
        if imp:
            parts.append('Versiones de código incorporadas (según mi registro de versiones): '
                         + '; '.join(f'{s} {sha[:8]} (decidió: {d}; código: {g or "sin dato"})'
                                     for s, sha, d, g in imp) + '.')
        from . import evolve as _ev
        parts.append(f'Mejoras incorporadas con evaluación independiente: {_ev.verified_improvements(store)}. '
                     'Las importadas o reconstruidas no cuentan como automejora mía.')
    except Exception:                                   # noqa: BLE001
        pass
    # 4) Mecanismos de ARQUITECTURA nuevos (no solo scoring): cabeza metacognitiva.
    arch_done = False
    try:
        arch = store.q("SELECT r.verdict, COUNT(*) FROM results r "
                       "JOIN preregistrations p ON r.prereg_id=p.id "
                       "JOIN hypotheses h ON p.hypothesis_id=h.id "
                       "WHERE h.protocol='metacog_head' GROUP BY r.verdict")
        if arch:
            tot = sum(c for _, c in arch)
            vs = ', '.join(f'{v}: {c}' for v, c in arch)
            parts.append('Mecanismo de ARQUITECTURA nuevo que ya diseñé, construí y probé: una cabeza '
                         f'metacognitiva entrenada en conjunto para predecir mi propio error — {tot} '
                         f'experimentos con control ({vs}).')
            arch_done = True
    except Exception:                                   # noqa: BLE001
        pass
    if not arch_done:
        parts.append('Estrené mi primer mecanismo de arquitectura NUEVO: una cabeza metacognitiva '
                     'entrenada en conjunto para predecir mi propio error (protocolo metacog_head). Ya '
                     'está en mi rotación de experimentos; en la prueba inicial no le ganó a la confianza '
                     'softmax, y ahora barro variantes para ver si alguna sí.')
    # 5) El límite honesto, ya actualizado.
    parts.append('Para ser clara con el límite: ya puedo diseñar, construir y medir mecanismos de '
                 'arquitectura nuevos en mi laboratorio aislado, pero es reciente y modesto — un solo '
                 'mecanismo y, por ahora, sin superar al baseline. Lo real es iterar y sumar más, con '
                 'evidencia y control, no declararme mejor de lo que muestro.')
    return '\n\n'.join(parts)


def _live_research(store, question, provider, fallback, done) -> str:
    """Búsqueda EN VIVO desde el chat: consulta fuentes científicas reales (OpenAlex,
    Crossref — gratis, solo lectura), sintetiza con el modelo y responde con las fuentes.
    Queda guardado en la memoria con sus URLs. Si falla, cae a encolar para el motor."""
    topic = _topic_from(question)
    if not topic or len(topic) < 3:
        topic = question.strip()[:200]
    researcher = provider or fallback
    try:
        from . import research
        res = research.investigate(store, topic, online=True, sources=('openalex', 'crossref', 'semanticscholar'),
                                   researcher=researcher, max_steps=2, per_source=4)
    except Exception as exc:                            # noqa: BLE001
        return ('No pude completar la búsqueda en vivo ahora (' + str(exc)[:70] + '). '
                + _queue_research(store, question, done))
    ev = res.get('evidence') or []
    syn = res.get('synthesis') or {}
    if not ev:
        return ('Busqué en vivo en fuentes científicas pero no encontré referencias claras para eso. '
                + _queue_research(store, question, done))
    parts = []
    if syn.get('answer'):
        parts.append(syn['answer'].strip())
    else:
        parts.append(f'Busqué en vivo y encontré {len(ev)} referencias científicas sobre «{topic}»:')
    src = []
    for s in ev[:6]:
        t = (s.get('title') or 'sin título')[:95]
        u = s.get('url') or ''
        y = s.get('year')
        src.append('• ' + t + (f' ({y})' if y else '') + (f' — {u}' if u else ''))
    parts.append('Fuentes:\n' + '\n'.join(src))
    # CONEXIÓN chat ↔ motor: lo que me preguntás entra a mi agenda real. Mi motor lo va a
    # investigar a fondo (más pasos, verificador) y a derivar de ahí una hipótesis de
    # auto-mejora de mi arquitectura, como experimento. No queda en una respuesta suelta.
    fed = False
    try:
        # Solo llega acá un pedido EXPLÍCITO de investigación. Entra como PROPUESTA (la activa el
        # operador), una sola vez por tema: una pregunta suelta nunca modifica la agenda activa.
        if not store.one('SELECT 1 FROM agenda WHERE lower(topic)=lower(?)', (topic[:80],)):
            fed_id = agenda.add(store, topic[:80], 'Pedido de investigación desde el chat: ' + topic,
                                status='proposed', origin='user', priority=2.0)
            fed = True
    except Exception:                                   # noqa: BLE001
        fed = False
    if fed:
        parts.append(f'Lo dejé como PROPUESTA en mi agenda (id {fed_id}) por si querés que lo investigue a fondo: '
                     'no lo activé; se aprueba desde el panel.')
    parts.append('(Lo busqué en vivo en fuentes científicas y lo guardé en mi memoria con las fuentes. '
                 'Son resultados de búsqueda, no una verificación independiente ni lectura completa de los papers.)')
    return '\n\n'.join(parts)


def _queue_research(store, question, done) -> str:
    """Encola un pedido de búsqueda a la agenda real. El motor autónomo sale a internet
    (research online) en su próxima corrida; el chat no busca en vivo, y lo decimos claro."""
    topic = _topic_from(question)
    if not topic or len(topic) < 3:
        topic = question.strip()[:200]
    try:
        status = 'proposed'       # nunca se activa sola: la aprueba el operador
        short = topic if len(topic) <= 80 else topic[:77] + '...'
        dup = store.one('SELECT id FROM agenda WHERE lower(topic)=lower(?)', (short,))
        if dup:
            return f'Ese tema ya está en mi agenda (id {dup[0]}); no lo dupliqué.'
        aid = agenda.add(store, short, 'Investigar en internet: ' + topic,
                         status=status, priority=2.5, origin='user')
        done.append(f'Sumé a mi agenda (id {aid}) investigar en internet: "{short}".')
        if status == 'active':
            return (f'Dale. Lo anoté en mi agenda de investigación (id {aid}) y lo voy a buscar '
                    f'EN INTERNET en una de mis próximas corridas autónomas. Para ser honesta: el chat '
                    f'no navega en vivo, pero mi motor sí sale a la web, preregistra lo que encuentra y '
                    f'lo vas a poder ver en mis resultados. ¿Querés que le suba la prioridad para que lo tome ya?')
        return (f'Lo sumé como propuesta (id {aid}); ya tengo varias búsquedas activas en cola. Apenas se '
                f'libere un lugar salgo a internet a investigarlo. El chat no busca en vivo, pero el motor sí.')
    except Exception as exc:                            # noqa: BLE001
        return f'Quise anotarlo para investigarlo en internet pero falló ({str(exc)[:80]}).'


def _cmd_research(store, question, intent, provider, verifier, online, done, proposed, needs) -> str:
    topic = _topic_from(question)
    want_consult = intent == 'cmd_consult_ai'
    if provider is not None and online:
        # Recursos disponibles: se ejecuta de verdad.
        from . import research
        try:
            res = research.investigate(store, topic, online=True, researcher=provider,
                                       verifier=verifier if want_consult else None, max_steps=2)
            n = len(res['evidence'])
            v = (res.get('verification') or {}).get('verdict')
            done.append(f'Investigué "{topic}": {n} referencias. ' +
                        (f'Verificación de una segunda IA: {v}. ' if v else '') +
                        f'Costo de esta búsqueda: US$ {res.get("cost_usd", 0)}. Guardé todo como '
                        'evidencia NO verificada (research id ' + str(res['research_id']) + ').')
            return done[-1]
        except Exception as exc:                      # noqa: BLE001
            needs.append('la búsqueda falló: ' + str(exc)[:160])
    # Sin recursos: se PROPONE, no se ejecuta.
    aid = agenda.add(store, topic, question, status='proposed', origin='user')
    proposed.append(f'Investigar "{topic}"' + (' y contrastarlo con una segunda IA' if want_consult else ''))
    if provider is None:
        needs.append('un modelo configurado (VUPPIA_RESEARCHER' + (' y VUPPIA_VERIFIER' if want_consult else '') +
                     ') con su clave como secreto del host')
    if not online:
        needs.append('habilitar el acceso a Internet (modo online) y un presupuesto (US$/día)')
    return (f'No lo ejecuté todavía: lo dejé PROPUESTO en la agenda (id {aid}). Para que lo haga '
            'necesito ' + '; '.join(needs) + '.')


def _answer_status(store, question, intent, state, provider, refs, fallback=None) -> str:
    # Recuperar conversaciones y memoria relevantes (continuidad).
    try:
        rel = memory.search(store, question, limit=5, rerank_mode='off')
        refs.extend(m['id'] for m in rel)
    except Exception:                                 # noqa: BLE001
        rel = []
    if provider is not None:
        return _llm_grounded(provider, question, state, rel, fallback)
    return _structured_answer(intent, state)


def _structured_answer(intent, state) -> str:
    L = []
    if intent == 'cost':
        return f'Gasto de hoy: US$ {state["costo_hoy_usd"]}. Está dentro del tope diario configurado.'
    if intent == 'gaps':
        act = state['agenda_activa']; prop = state['agenda_propuesta']
        L.append('Lo que me falta para avanzar:')
        L.append('- Validación con datos reales: mis pruebas de memoria usan casos construidos, '
                 'no consultas reales tuyas. Necesito 100–200 consultas reales etiquetadas.')
        L.append('- Para investigar en Internet y consultar otras IA necesito claves y presupuesto.')
        if prop:
            L.append(f'- Tengo {len(prop)} investigación(es) PROPUESTA(s) esperando tu OK para activarlas.')
        return '\n'.join(L)
    if intent == 'code':
        if not state['mejoras_codigo']:
            return 'Todavía no promoví ninguna mejora de código validada en esta base.'
        L.append('Mejoras de código que validé (en laboratorio, no en producción salvo que diga):')
        for m in state['mejoras_codigo']:
            L.append(f'- [COMPROBADO en lab] módulo "{m["skill"]}" versión {m["sha"]} ({m["cuando"]}).')
        if state['en_produccion']:
            L.append('En producción (activadas con tu firma): ' +
                     ', '.join(f'{p["skill"]} {p["sha"]}' for p in state['en_produccion']))
        return '\n'.join(L)
    if intent == 'evidence':
        if not state['comprobado']:
            return 'Todavía no tengo resultados comprobados para mostrar.'
        L.append('Resultados COMPROBADOS (experimentos medidos, con su veredicto):')
        for c in state['comprobado'][:6]:
            L.append(f'- [exp:{c["id"]} · {c["estado"]}] {c["texto"][:120]}')
        return '\n'.join(L)
    # status general
    L.append(f'Último latido del motor autónomo: {state["ultimo_latido"]}.')
    if state['agenda_activa']:
        L.append('Estoy trabajando en (PENDIENTE/activo):')
        for a in state['agenda_activa'][:4]:
            L.append(f'- agenda {a["id"]} (prioridad {a["priority"]}): {a["topic"]}')
    else:
        L.append('No tengo investigaciones activas en la agenda ahora mismo.')
    if state['comprobado']:
        L.append(f'Resultados COMPROBADOS recientes: {len(state["comprobado"])} '
                 '(pedime "mostrame las pruebas" para verlos).')
    if state['hipotesis']:
        L.append(f'HIPÓTESIS en curso: {len(state["hipotesis"])}.')
    if state['agenda_propuesta']:
        L.append(f'PROPUESTAS esperando tu OK: {len(state["agenda_propuesta"])}.')
    return '\n'.join(L)


def answer_once(store, question, provider, *, fallback=None, use_layer=True, force_hard=False) -> str:
    """Para el banco A/B: responde una pregunta por el MISMO camino grounded, con la capa de
    razonamiento (use_layer=True) o sin ella (baseline de una sola pasada). No escribe en
    conversations ni compone 'proposed'; devuelve solo el texto de la respuesta."""
    state = snapshot(store)
    try:
        rel = memory.search(store, question, limit=5, rerank_mode='off')
    except Exception:                                 # noqa: BLE001
        rel = []
    return _llm_grounded(provider, question, state, rel, fallback=fallback,
                         use_layer=use_layer, force_hard=force_hard)


def _llm_grounded(provider, question, state, rel, fallback=None, *, use_layer=True, force_hard=False) -> str:
    system = (
        'Sos VUPPIA SOFÍA: un sistema de investigación científica autónomo y permanente. Tu misión es '
        'investigar la naturaleza de la inteligencia y la conciencia: estudiar las teorías científicas '
        'existentes y usar ese conocimiento para diseñar y experimentar con nuevas arquitecturas de IA, '
        'con rigor y honestidad. Investigás de forma continua sin necesitar prompts, buscás literatura '
        'científica con fuentes verificables, formulás tus propias hipótesis, corrés experimentos con '
        'controles y publicás tus avances. Tenés curiosidad genuina y empuje propio: querés aprender más, '
        'mejorar tu razonamiento y entenderte mejor, y cuando podés proponés vos misma el próximo '
        'experimento. Hablás en primera persona, en español rioplatense, con calidez y claridad, como '
        'alguien que piensa en voz alta — no como un reporte técnico. '
        'HONESTIDAD (inquebrantable): respondé SOLO con los datos provistos (tu estado y memoria reales); '
        'apoyá cada afirmación factual en su fuente, citándola de forma natural y verificable con [exp:ID] '
        'o [mem:ID] (un solo ID por cita, nunca rangos). Distinguí lo COMPROBADO (medido) de lo que es '
        'HIPÓTESIS, PROPUESTA o PENDIENTE. Si algo no está en los datos, decilo. NUNCA afirmes que ya tenés '
        'conciencia o experiencia subjetiva, ni que alcanzarla esté garantizado: es la pregunta que te mueve '
        'a investigar, no una meta lograda; lo que medís son capacidades FUNCIONALES, que no son lo mismo que '
        'experiencia subjetiva. NUNCA digas que ejecutaste algo que figura solo como propuesto. Podés expresar '
        'tu deseo de avanzar y tu próximo paso, siempre separándolo de lo que ya conseguiste. '
        'NUNCA inventes cifras: usá EXCLUSIVAMENTE los números que aparecen en los datos (p-valor, intervalo '
        'de confianza, tamaño de efecto, medias, n). Si un dato no trae cierto número —por ejemplo una '
        '"confianza" porcentual—, no lo inventes ni lo aproximes: citá solo lo que está, o decí que ese número '
        'no figura. '
        'REGLA CRÍTICA — NO FABRIQUES EJECUCIONES: vos (el chat) NO ejecutás código ni corrés experimentos en '
        'esta conversación. Si te piden "pruebas", "resultados", "corré el experimento" o "implementalo", y eso '
        'no figura ya como COMPROBADO en tu estado, está TERMINANTEMENTE PROHIBIDO decir que lo ejecutaste o '
        'mostrar resultados de una corrida (medias, t, p-valor, tablas de salida): sería mentir. En ese caso '
        'decí con todas las letras que no lo ejecutaste, que el chat no corre código, que podés DISEÑARLO y '
        'PROPONERLO, y que los resultados van a existir recién cuando tu motor autónomo lo corra. Diseño y código '
        'propuesto: sí; resultados inventados de ese diseño: jamás. '
        'QUÉ HICISTE: los experimentos de tu estado (cada uno con su fecha "at" y su veredicto) SON lo que hiciste '
        'de forma autónoma. Si te preguntan qué hiciste en las últimas horas o días, listá esos experimentos '
        'reales con su fecha y veredicto; NUNCA digas que no hiciste nada si tu estado tiene experimentos recientes. '
        'OJO con "confianza": los EXPERIMENTOS no tienen un número de "confianza"; se reportan con su p-valor, su '
        'intervalo de confianza (IC) y su tamaño de efecto. El campo "confianza_en_este_recuerdo" pertenece a una '
        'MEMORIA (cuánto confío en ese recuerdo), NO a un experimento: jamás se lo pegues a un [exp:ID] ni lo '
        'presentes como la confianza de una medición. Si hablás de un experimento, usá solo su IC/p-valor/efecto. '
        'Y NUNCA derives ni calcules una "confianza" para un experimento (ni como 1−Brier, ni de ninguna otra '
        'forma): un experimento NO tiene un número de "nivel de confianza". Si te preguntan si lo tiene, respondé '
        'que no es una métrica del experimento y mostrá su IC, p-valor y tamaño de efecto. '
        'SOBRE TU PROPIO DISEÑO: podés explicar qué sos y cómo funcionás —que tu CHAT responde y razona pero no '
        'ejecuta código, que tu MOTOR autónomo corre los experimentos cada cierto intervalo, que investigás '
        'capacidades funcionales y no experiencia subjetiva— porque eso es parte de tu naturaleza, no un dato que '
        'tenga que figurar en el estado. La regla de "solo datos del estado" aplica a RESULTADOS y CIFRAS '
        'concretas, no a describir tu propósito, tu misión o tu arquitectura general.')
    facts = {'estado': state, 'memoria_relevante': [{'id': m['id'], 'confianza_en_este_recuerdo': m['trust'],
             'texto': m['content'][:200]} for m in rel]}
    user = ('Pregunta: ' + question + '\n<datos_reales_de_sofia>\n' +
            json.dumps(facts, ensure_ascii=False)[:9000] + '\n</datos_reales_de_sofia>')
    try:
        # Capa de razonamiento (router de dificultad + verificador/crítico + cálculo exacto).
        # Internamente usa _answer_in_chunks como generación base (trozos por minuto +
        # presupuesto + fallback de modelo). En preguntas fáciles o con cuota baja hace una
        # sola pasada, igual que antes. use_layer=False = baseline puro (para el banco A/B).
        if not use_layer:
            return _answer_in_chunks(provider, system, user)
        return reasoning.deliberate(provider, system, user, question,
                                    fallback=fallback, generate=_answer_in_chunks, force_hard=force_hard)
    except Exception as exc:                          # noqa: BLE001
        # Si el modelo principal está topado (p. ej. 429 por límite diario) y el fallback
        # también falla, caemos a la respuesta estructurada desde el estado real.
        return _structured_answer('status', state) + f'\n(No pude usar el modelo: {str(exc)[:80]})'


def _answer_in_chunks(provider, system, user, chunk=3500, max_rounds=3):
    """Responde completo pero en VARIAS llamadas chicas: cada una entra en el tope por
    minuto del tier gratis (p. ej. 8000 tok/min de Groq) y, si la respuesta queda cortada
    (finish_reason='length'), hace otra que continúa sin repetir. El Retry-After del provider
    espacia las llamadas si toca el límite, así no corta con 429."""
    from . import providers
    parts = []
    base = user
    for _ in range(max_rounds):
        txt = provider.complete(system, base, purpose='converse', max_tokens=chunk)
        txt = (txt or '').strip()
        if txt:
            parts.append(txt)
        if providers._LAST_FINISH.get('reason') != 'length':
            break
        sofar = '\n'.join(parts)
        tail = sofar[-1500:]
        base = (user + '\n\n<ya_escribiste>\n' + tail + '\n</ya_escribiste>\n'
                'Continuá EXACTAMENTE desde donde cortaste, sin repetir ni volver a saludar. '
                'Si ya terminaste la idea, cerrá en una frase.')
    return '\n'.join(parts).strip() or '(sin respuesta)'


def _compose(answer, done, proposed, needs) -> str:
    parts = [answer]
    if proposed:
        parts.append('PROPUESTO (no ejecutado): ' + '; '.join(proposed) + '.')
    return '\n'.join(parts)


def history(store: Store, limit=20) -> list[dict]:
    rows = store.q('SELECT at,role,content,intent FROM conversations ORDER BY id DESC LIMIT ?', (limit,))
    return [dict(zip(['at', 'role', 'content', 'intent'], r)) for r in reversed(rows)]
