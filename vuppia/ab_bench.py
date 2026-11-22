"""Banco A/B de la capa de razonamiento.

Compara, sobre un set FIJO de preguntas dificiles con respuesta VERIFICABLE, la respuesta
del chat SIN la capa (baseline de una sola pasada) contra CON la capa (router + protocolo +
calculo exacto + verificador forzado). Mide:
  - acierto: la respuesta contiene el numero/criterio esperado (tolerancia numerica),
  - tokens reales: delta de SUM(input+output) en llm_calls, filtrado por el modelo del chat
    (no se mezcla con el motor, que usa otro modelo),
  - errores 429 acumulados.

Las preguntas son AUTOCONTENIDAS (toda la info esta en el enunciado): asi la regla de honestidad
"responder solo desde el estado" no las bloquea, y lo que se mide es el razonamiento, no memoria.
Esto NO prueba la calidad en preguntas abiertas (opinion, redaccion): eso no tiene test limpio y
no se evalua aca. Es la foto de la parte objetiva.
"""
from __future__ import annotations

import re

# id, pregunta, num (valor esperado), tol (tolerancia), kw (substrings alternativos opcionales)
BENCH = [
    {'id': 1,  'q': 'Si una mejora sube el score de 0.40 a 0.55, ¿cuál es la mejora relativa en porcentaje?', 'num': 37.5, 'tol': 0.8},
    {'id': 2,  'q': 'Un experimento tiene 200 casos y 30 fallan. ¿Qué porcentaje de acierto tiene?', 'num': 85, 'tol': 0.5},
    {'id': 3,  'q': 'Un modelo acierta 42 de 60 preguntas. ¿Qué porcentaje de acierto es?', 'num': 70, 'tol': 0.5},
    {'id': 4,  'q': 'Si cada tick del motor tarda 8 minutos y corren 5 ticks, ¿cuántos minutos en total?', 'num': 40, 'tol': 0.1},
    {'id': 5,  'q': 'Tengo 8000 tokens por minuto y cada respuesta usa 2000 tokens. ¿Cuántas respuestas completas entran por minuto?', 'num': 4, 'tol': 0.1},
    {'id': 6,  'q': '¿Cuánto es el 15% de 240?', 'num': 36, 'tol': 0.1},
    {'id': 7,  'q': 'El doble de un número más 7 da 31. ¿Cuál es el número? Resolvé paso a paso.', 'num': 12, 'tol': 0.1},
    {'id': 8,  'q': '¿Cuánto es la raíz cuadrada de 169?', 'num': 13, 'tol': 0.1},
    {'id': 9,  'q': 'Calculá 2 elevado a la 10.', 'num': 1024, 'tol': 0.5},
    {'id': 10, 'q': 'Un auto recorre 150 km en 2 horas. ¿Cuál es su velocidad promedio en km/h?', 'num': 75, 'tol': 0.5},
    {'id': 11, 'q': 'Si un valor sube de 0.47 a 0.63, ¿cuál es la diferencia absoluta?', 'num': 0.16, 'tol': 0.005},
    {'id': 12, 'q': '¿Cuánto da 12 por 15 más 30? Mostrá el cálculo.', 'num': 210, 'tol': 0.1},
    {'id': 13, 'q': 'Si de cada 4 respuestas 3 son correctas, ¿qué porcentaje de error hay?', 'num': 25, 'tol': 0.5},
    {'id': 14, 'q': 'Una mejora pasa de 50 a 80. ¿Cuál es el aumento relativo en porcentaje?', 'num': 60, 'tol': 0.8},
]

_NUM = re.compile(r'-?\d+(?:[.,]\d+)?')


def _nums(text: str):
    out = []
    for m in _NUM.finditer(text or ''):
        try:
            out.append(float(m.group().replace(',', '.')))
        except Exception:                               # noqa: BLE001
            pass
    return out


def _score(answer: str, item: dict) -> int:
    a = (answer or '').lower()
    if item.get('kw') and any(k.lower() in a for k in item['kw']):
        return 1
    if 'num' in item:
        tol = float(item.get('tol', 0.5))
        if any(abs(n - item['num']) <= tol for n in _nums(a)):
            return 1
    return 0


def _toksum(store, model: str) -> int:
    r = store.one('SELECT COALESCE(SUM(COALESCE(input_tokens,0))+SUM(COALESCE(output_tokens,0)),0) '
                  'FROM llm_calls WHERE model=?', (model,))
    return int(r[0] or 0)


def _n429(store) -> int:
    r = store.one("SELECT COUNT(*) FROM llm_calls WHERE status='error' AND error LIKE '%429%'")
    return int(r[0] or 0)


def run(store, provider, fallback=None, items=None) -> dict:
    from . import converse
    items = items or BENCH
    model = getattr(provider, 'model', '?')
    rows = []
    agg = {'n': 0, 'base_ok': 0, 'layer_ok': 0, 'base_tok': 0, 'layer_tok': 0}
    n429_0 = _n429(store)
    for it in items:
        q = it['q']
        t0 = _toksum(store, model)
        try:
            a_base = converse.answer_once(store, q, provider, fallback=fallback, use_layer=False)
        except Exception as e:                          # noqa: BLE001
            a_base = f'(error: {e})'
        bt = _toksum(store, model) - t0
        t1 = _toksum(store, model)
        try:
            a_layer = converse.answer_once(store, q, provider, fallback=fallback,
                                           use_layer=True, force_hard=True)
        except Exception as e:                          # noqa: BLE001
            a_layer = f'(error: {e})'
        lt = _toksum(store, model) - t1
        ob, ol = _score(a_base, it), _score(a_layer, it)
        agg['n'] += 1; agg['base_ok'] += ob; agg['layer_ok'] += ol
        agg['base_tok'] += bt; agg['layer_tok'] += lt
        rows.append({'id': it['id'], 'q': q[:70], 'exp': it.get('num', it.get('kw')),
                     'base_ok': ob, 'layer_ok': ol, 'base_tok': bt, 'layer_tok': lt})
    agg['n429_bench'] = _n429(store) - n429_0
    agg['model'] = model
    return {'ok': True, 'agg': agg, 'items': rows}
