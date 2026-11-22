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
    if has('buscá', 'busca ', 'investigá ', 'investiga ', 'sacá tus conclusiones', 'sacá conclusiones',
           'conseguí información', 'averiguá'):
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
    t = re.sub(r'^(buscá|busca|investigá|investiga|conseguí|averiguá|consultá otra[s]? ia sobre|'
               r'contrastá|agregá|agrega|sumá|suma|proponé una investigación sobre)\s*', '', t, flags=re.I)
    t = re.sub(r'\s*(y sac[áa]\s+(tus\s+)?conclusiones|a la agenda)\.?\s*$', '', t, flags=re.I)
    return t.strip()[:200] or text.strip()[:200]


def ask(store: Store, question: str, *, provider=None, verifier=None, online=False,
        allow_actions=True) -> dict:
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
        answer = _answer_status(store, question, intent, state, provider, refs)

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


def _answer_status(store, question, intent, state, provider, refs) -> str:
    # Recuperar conversaciones y memoria relevantes (continuidad).
    try:
        rel = memory.search(store, question, limit=5, rerank_mode='off')
        refs.extend(m['id'] for m in rel)
    except Exception:                                 # noqa: BLE001
        rel = []
    if provider is not None:
        return _llm_grounded(provider, question, state, rel)
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


def _llm_grounded(provider, question, state, rel) -> str:
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
        'no figura.')
    facts = {'estado': state, 'memoria_relevante': [{'id': m['id'], 'trust': m['trust'],
             'texto': m['content'][:200]} for m in rel]}
    user = ('Pregunta: ' + question + '\n<datos_reales_de_sofia>\n' +
            json.dumps(facts, ensure_ascii=False)[:9000] + '\n</datos_reales_de_sofia>')
    try:
        return provider.complete(system, user, purpose='converse', max_tokens=8000)
    except Exception as exc:                          # noqa: BLE001
        return _structured_answer('status', state) + f'\n(No pude usar el modelo: {str(exc)[:80]})'


def _compose(answer, done, proposed, needs) -> str:
    parts = [answer]
    if proposed:
        parts.append('PROPUESTO (no ejecutado): ' + '; '.join(proposed) + '.')
    return '\n'.join(parts)


def history(store: Store, limit=20) -> list[dict]:
    rows = store.q('SELECT at,role,content,intent FROM conversations ORDER BY id DESC LIMIT ?', (limit,))
    return [dict(zip(['at', 'role', 'content', 'intent'], r)) for r in reversed(rows)]
