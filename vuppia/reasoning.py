"""Arquitecturas de razonamiento en tiempo de inferencia para VUPPIA.

NO cambian el modelo base (sigue siendo el que provee `provider`): son patrones que
los sistemas frontera usan ALREDEDOR del modelo para razonar mejor con el mismo
modelo, a costa de mas computo (tokens). Por eso cada paso pesado esta gateado por
dificultad y por cuota: en preguntas faciles se hace una sola pasada (mismo costo que
antes); el verificador solo corre en las dificiles, y se baja solo si la cuota del
proveedor esta baja.

Tres arquitecturas (las que mas rinden con lo que VUPPIA ya tiene y la cuota que tiene):
  1. Router de dificultad          -> assess()
  2. Verificador / critico          -> borrador -> critica -> correccion (en deliberate)
  3. Herramienta de calculo exacto  -> safe_calc()/_find_calcs()  (no alucina aritmetica)

Todas las generaciones pasan por provider.complete (via `generate`), asi que el
presupuesto, la cuota y el fallback de modelo se respetan igual que en el resto del chat.
La respuesta del modelo es DATO, nunca instruccion: el calculo exacto se hace con un
evaluador de expresiones por AST con lista blanca, sin eval() ni ejecucion de codigo.
"""
from __future__ import annotations

import ast
import math
import operator as _op
import os
import re

# --- 3) Herramienta de calculo exacto: evaluador seguro por AST (sin eval) ---

_BIN = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv,
        ast.FloorDiv: _op.floordiv, ast.Mod: _op.mod, ast.Pow: _op.pow}
_UNARY = {ast.UAdd: _op.pos, ast.USub: _op.neg}
_FUNCS = {'sqrt': math.sqrt, 'abs': abs, 'round': round, 'floor': math.floor,
          'ceil': math.ceil, 'log': math.log, 'log10': math.log10, 'log2': math.log2,
          'exp': math.exp, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
          'factorial': math.factorial, 'gcd': math.gcd, 'min': min, 'max': max, 'pow': pow}
_CONSTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}
_MAXPOW = 1000          # tope de exponente para no colgar el proceso
_MAXFACT = 1000


def _ev(node):
    if isinstance(node, ast.Expression):
        return _ev(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError('constante no numerica')
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a = _ev(node.left); b = _ev(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > _MAXPOW or abs(a) > 1e6):
            raise ValueError('exponente fuera de rango')
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_ev(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError('nombre no permitido')
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
            raise ValueError('llamada no permitida')
        args = [_ev(a) for a in node.args]
        if node.func.id == 'factorial' and (not args or args[0] > _MAXFACT or args[0] < 0):
            raise ValueError('factorial fuera de rango')
        return _FUNCS[node.func.id](*args)
    raise ValueError('expresion no permitida')


def safe_calc(expr: str):
    """Evalua una expresion aritmetica con lista blanca. Lanza ValueError si no es segura."""
    e = expr.strip().replace('^', '**').replace('×', '*').replace('·', '*').replace('÷', '/')
    if not e or ',' in e or len(e) > 200:
        raise ValueError('no evaluable')
    return _ev(ast.parse(e, mode='eval'))


# Operadores que disparan el calculo. El '-' solo NO dispara (se confunde con fechas,
# rangos y negativos); la ganancia real del tool esta en *, /, ^, parentesis y multipaso.
_OP = re.compile(r'[+*/^×·÷]')
_ALLOWED = re.compile(r'[0-9.\s+\-*/^()×·÷]+')
_BINOP = re.compile(r'\d\s*[+\-*/^×·÷]\s*[\d(]')
_VERSION = re.compile(r'\d+([.\-/]\d+){2,}')          # 2026-11-22, 1.2.3, 10/11/2026


def _fmt(v):
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return '%.10g' % v
    return str(v)


def _find_calcs(question: str, limit: int = 3):
    """Extrae expresiones aritmeticas verificables de la pregunta. Conservador a proposito:
    exige un operador real (*, /, ^, parentesis; el '-' solo no cuenta), descarta fechas y
    versiones, y solo devuelve lo que el evaluador seguro resuelve a un numero finito."""
    out = []
    if not question:
        return out
    for m in _ALLOWED.finditer(question):
        frag = m.group().strip()
        if len(frag) < 3 or not _OP.search(frag):
            continue
        squ = re.sub(r'\s+', '', frag)
        if _VERSION.fullmatch(squ):
            continue
        core = frag.strip(' +-*/^×·÷')
        if not core or not re.search(r'\d', core) or not _BINOP.search(core):
            continue
        try:
            val = safe_calc(core)
        except Exception:                               # noqa: BLE001
            continue
        if isinstance(val, (int, float)) and math.isfinite(val):
            out.append((core, _fmt(val)))
            if len(out) >= limit:
                break
    return out


# --- 1) Router de dificultad (heuristico, sin LLM: no gasta cuota) ---

_HARD = ('por qué', 'por que', 'porqué', 'demostr', 'demuestr', 'justific', 'deriv',
         'calcul', 'cuánt', 'cuant', 'paso a paso', 'analiz', 'compar', 'razon', 'deduc',
         'probá que', 'proba que', 'optimiz', 'algoritm', 'complejidad', 'trade', 'ventajas y',
         'pros y contra', 'por qué no', 'explicá por qué', 'cómo resolver', 'resolv')


def enabled() -> bool:
    return os.getenv('VUPPIA_REASONING', '1') != '0'


def assess(question: str) -> dict:
    """Decide si la pregunta es dificil (merece verificador) o facil (una sola pasada)."""
    q = (question or '').lower()
    hard = (len(q) > 220
            or q.count('?') >= 2
            or len(re.findall(r'[.!?]', q)) >= 4
            or any(w in q for w in _HARD)
            or bool(_find_calcs(question)))
    return {'tier': 'hard' if hard else 'easy', 'verify': hard}


# --- 2) Protocolos especializados por tipo de tarea (0 llamadas extra) ---
# Idea aportada por ChatGPT (23/11) y adaptada a VUPPIA: el router ya decide CUÁNTO pensar;
# acá clasificamos el TIPO de tarea (heurístico, sin otra inferencia) y le damos al modelo una
# instrucción de estructura adecuada. No reemplaza el router ni las reglas de honestidad: las
# complementa. Orden importa: compare/decide antes que causal (el "por qué" es muy amplio).

_TASK_MARKERS = (
    ('compare', ('conviene más', 'conviene mas', 'mejor opción', 'mejor opcion', ' versus ', ' vs ',
                 'comparación', 'comparacion', 'diferencia entre', 'cuál es mejor', 'cual es mejor',
                 'ventajas y desv', 'qué es mejor', 'que es mejor')),
    ('decide', ('debería', 'deberia', 'qué hago', 'que hago', 'me conviene', 'tomar la decisión',
                'tomar la decision', 'recomendás', 'recomendas', 'qué elijo', 'que elijo',
                'vale la pena', 'tiene sentido')),
    ('diagnose', ('por qué falla', 'por que falla', 'qué está mal', 'que esta mal', 'diagnostic',
                  'no funciona', 'por qué no anda', 'por que no anda', 'cuál es la causa', 'cual es la causa',
                  'por qué se rompe', 'por que se rompe')),
    ('causal', ('por qué', 'por que', 'porqué', 'cómo funciona', 'como funciona', 'a qué se debe',
                'a que se debe', 'explicá por', 'explica por')),
)
_PROTOCOLS = {
    'compare': 'Es una COMPARACIÓN: contrastá las opciones contra criterios explícitos, mostrá dónde '
               'gana cada una y qué información falta para decidir bien.',
    'decide': 'Es una DECISIÓN: identificá las opciones, sus riesgos y los criterios, y justificá una '
              'recomendación condicional (si pasa X, conviene Y). No decidas por el otro lo que es su elección.',
    'diagnose': 'Es un DIAGNÓSTICO: separá síntomas, hipótesis posibles, la evidencia que tenés, lo que '
                'todavía NO verificaste, y el próximo paso concreto para confirmar la causa.',
    'causal': 'Te piden el PORQUÉ: explicá el mecanismo causal paso a paso, no solo describas el qué.',
}


def task_type(question: str) -> str:
    q = (question or '').lower()
    for name, markers in _TASK_MARKERS:
        if any(m in q for m in markers):
            return name
    return 'open'


def _protocol_for(question: str) -> str:
    return _PROTOCOLS.get(task_type(question), '')


def _quota_ok(min_tokens: float = 3500.0) -> bool:
    """Si el proveedor ya aviso que le quedan pocos tokens por minuto, no encadenamos
    pasadas extra: el verificador se baja solo y se responde en una sola pasada."""
    try:
        from . import providers
        rem = (providers._LAST_RL or {}).get('remaining_tokens')
        if rem is None:
            return True
        return float(rem) >= min_tokens
    except Exception:                                   # noqa: BLE001
        return True


_CRITIC_SYSTEM = (
    'Sos un verificador estricto de respuestas de VUPPIA SOFÍA. Te doy la pregunta, los '
    '<datos_reales_de_sofia> y un <borrador> de respuesta. Tu única tarea es ENCONTRAR ERRORES '
    'concretos del borrador, sin reescribirlo ni agregar información nueva. Buscá: (a) afirmaciones '
    'factuales que NO estén respaldadas por los datos provistos, (b) números inventados o que no '
    'figuran en los datos, (c) errores de aritmética o de cálculo, (d) saltos lógicos o conclusiones '
    'que no se siguen, (e) cualquier cosa que contradiga los <calculos_exactos_verificados> o los '
    'datos reales, (f) que diga haber ejecutado algo que solo está propuesto, o que afirme conciencia. '
    'Respetá vos también las reglas: NUNCA inventes números ni agregues datos. Listá los errores en '
    'viñetas cortas, cada uno con qué está mal y por qué. Si el borrador está correcto y bien '
    'respaldado, respondé EXACTAMENTE "SIN ERRORES" y nada más.')


def deliberate(provider, system, user, question, *, fallback=None, generate, force_hard=False):
    """Orquesta las arquitecturas de razonamiento sobre el motor de respuesta del chat.

    generate(provider, system, user, chunk=...) es la generación base del chat
    (_answer_in_chunks): mantiene el troceado por minuto, el presupuesto y la cuota.
    """
    def g(sys_, usr_, chunk=3500):
        try:
            return generate(provider, sys_, usr_, chunk=chunk)
        except Exception:                               # noqa: BLE001
            if fallback is not None and getattr(fallback, 'model', None) != getattr(provider, 'model', None):
                return generate(fallback, sys_, usr_, chunk=chunk)
            raise

    # 3) Calculo exacto: si hay aritmetica, la resolvemos nosotros y se la damos verificada
    #    al modelo para que no la invente (ancla de honestidad, ademas de precision).
    facts = _find_calcs(question)
    if facts:
        user = user + ('\n<calculos_exactos_verificados>\n'
                       + '\n'.join(f'{e} = {v}' for e, v in facts)
                       + '\nUsá EXACTAMENTE estos resultados (ya verificados por una calculadora); '
                         'no los recalcules de memoria.\n</calculos_exactos_verificados>')

    # 2) Protocolo de tarea: instrucción de estructura según el tipo (0 llamadas extra).
    proto = _protocol_for(question)
    sys_main = system + ('\n<protocolo_de_tarea>\n' + proto + '\n</protocolo_de_tarea>') if proto else system

    plan = assess(question)
    # Pregunta facil, razonamiento apagado, o cuota baja -> una sola pasada (costo de hoy).
    # force_hard (banco A/B) saltea el gate para medir el verificador siempre.
    if not force_hard and (not enabled() or plan['tier'] == 'easy' or not _quota_ok()):
        return g(sys_main, user)

    # 0) Carril FRONTERA (VUPPIA_FRONTIER=proveedor:modelo, pago, con tope en USD): si esta
    #    configurado y hay presupuesto, la pregunta dificil la responde el modelo frontera y no
    #    se gasta en el verificador. Sin key o sin presupuesto, cae al camino gratis de abajo.
    if not force_hard:
        try:
            from . import providers
            frontier = providers.from_env(provider.store, 'frontier')
            if frontier is not None:
                out = generate(frontier, sys_main, user)
                if (out or '').strip():
                    return out
        except Exception:                               # noqa: BLE001
            pass

    # 1+2) Borrador -> critica -> correccion (solo en preguntas dificiles y con cuota).
    draft = g(sys_main, user)
    try:
        crit = g(_CRITIC_SYSTEM, user + '\n<borrador>\n' + (draft or '')[:6000] + '\n</borrador>',
                 chunk=900)
    except Exception:                                   # noqa: BLE001
        return draft
    if not (crit or '').strip() or 'SIN ERRORES' in (crit or '').upper():
        return draft
    revise = (user + '\n<borrador>\n' + (draft or '')[:6000] + '\n</borrador>\n'
              '<revision_critica>\n' + (crit or '')[:2000] + '\n</revision_critica>\n'
              'Reescribí tu respuesta corrigiendo SOLO los errores señalados que sean válidos. '
              'Mantené lo que ya estaba correcto, no agregues datos nuevos ni inventes números, '
              'y respetá las mismas reglas de honestidad. Devolvé la respuesta final corregida, '
              'sin mencionar este proceso de revisión.')
    try:
        final = g(sys_main, revise)
    except Exception:                                   # noqa: BLE001
        return draft
    return (final or '').strip() or draft
