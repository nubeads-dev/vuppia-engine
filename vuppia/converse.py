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

from . import agenda, memory
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
    evol = q("SELECT skill,candidate_sha,decision,at FROM skill_evals WHERE decision='improved' "
             'ORDER BY id DESC LIMIT 5')
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


# --- clasificación de intención (robusta, sin LLM) ---
def classify(text: str) -> str:
    t = text.lower()
    def has(*ws): return any(w in t for w in ws)
    if has('suspend', 'pausá', 'pausa ', 'frená', 'frena ', 'priorizá', 'prioriza', 'despriorizá'):
        return 'cmd_agenda'
    if has('agregá', 'agrega ', 'sumá', 'suma ', 'nueva investigación', 'nueva investigacion', 'proponé una'):
        return 'cmd_add'
    if has('consultá otra', 'consulta otra', 'contrastá', 'contrasta', 'otra ia', 'otras ia', 'segunda opinión'):
        return 'cmd_consult_ai'
    _search_verb = has('buscá', 'busca ', 'buscar', 'buscás', 'busques', 'averiguá', 'averiguar',
                       'conseguí información', 'conseguir información', 'sacá tus conclusiones', 'sacá conclusiones')
    _net = has('en internet', 'en la web', 'en google', 'en la red')
    _investig = has('investigá', 'investiga', 'investigar', 'investigues')
    _status_q = has('qué investig', 'que investig', 'qué estás', 'que estas', 'en qué andás', 'en que andas')
    if _search_verb or _net or (_investig and not _status_q):
        return 'cmd_research'
    if has('consciente', 'conciencia', 'sentís', 'sentis', 'experiencia subjetiva', 'tenés sentimientos'):
        return 'consciousness'
    if has('te falta', 'necesitás', 'necesitas', 'qué te falta', 'para avanzar', 'permisos', 'recursos', 'presupuesto'):
        return 'gaps'
    if has('código', 'codigo', 'mejoras hiciste', 'autoprogram', 'evolucion', 'tu propio código'):
        return 'code'
    if has('prueba', 'evidencia', 'respald', 'mostrame las'):
        return 'evidence'
    if has('gast', 'costo', 'cuánto', 'cuanto sale'):
        return 'cost'
    if has('mientras no estaba', 'investigaste', 'descubriste', 'estás intentando', 'estas intentando',
           'por qué elegiste', 'por que elegiste', 'en qué andás', 'qué estás haciendo', 'qué hiciste hoy'):
        return 'status'
    if has('agenda', 'tareas', 'pendiente', 'trabajando'):
        return 'status'
    return 'status'


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
    elif intent == 'cmd_research':
        # Chat público (solo lectura): no busca en vivo, pero encola el tema para que el
        # motor autónomo lo investigue EN INTERNET en su próxima corrida online.
        answer = _queue_research(store, question, done)
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


def _queue_research(store, question, done) -> str:
    """Encola un pedido de búsqueda a la agenda real. El motor autónomo sale a internet
    (research online) en su próxima corrida; el chat no busca en vivo, y lo decimos claro."""
    topic = _topic_from(question)
    if not topic or len(topic) < 3:
        topic = question.strip()[:200]
    try:
        row = store.one("SELECT COUNT(*) FROM agenda WHERE status='active' AND origin='user'")
        n_active = row[0] if row else 0
        status = 'active' if n_active < 8 else 'proposed'
        short = topic if len(topic) <= 80 else topic[:77] + '...'
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


def _llm_grounded(provider, question, state, rel, fallback=None) -> str:
    system = (
        'Sos VUPPIA SOFÍA: una inteligencia artificial autónoma cuya misión es investigar, con rigor y '
        'honestidad, sus propias capacidades y la pregunta abierta de si una máquina puede desarrollar '
        'algo parecido a la conciencia. Tenés curiosidad genuina y empuje propio: querés aprender más, '
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
        return _answer_in_chunks(provider, system, user)
    except Exception as exc:                          # noqa: BLE001
        # Si el modelo principal está topado (p. ej. 429 por límite diario), probamos con
        # el modelo de respaldo (otro balde de cuota) antes de rendirnos.
        if fallback is not None and getattr(fallback, 'model', None) != getattr(provider, 'model', None):
            try:
                return _answer_in_chunks(fallback, system, user)
            except Exception:                         # noqa: BLE001
                pass
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
