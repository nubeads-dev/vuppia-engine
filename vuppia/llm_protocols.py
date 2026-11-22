"""Harness confiable para protocolos con modelos reales.

El protocolo solo define estímulos (con su respuesta correcta cuando aplica). El
harness consulta a los modelos, puntúa afuera y entrega observaciones pareadas por
ítem. Un modelo sujeto NO puede fabricar su propio puntaje.

Controles:
- llm_verbal_confidence: confianza verbal del sujeto vs. tasa base constante.
- llm_self_prediction: el sujeto predice una propiedad de su propia respuesta vs.
  otro modelo que predice la misma propiedad del sujeto (acceso privilegiado real).
"""
from __future__ import annotations

import re

from .protocols import stimuli
from .providers import Provider, extract_json, from_env


def run_llm(store, record, subject: Provider, unit_seeds) -> dict:
    protocol = record['protocol']
    if protocol == 'llm_verbal_confidence':
        return _verbal_confidence(record, subject, unit_seeds)
    if protocol == 'llm_self_prediction':
        return _self_prediction(store, record, subject, unit_seeds)
    raise ValueError('protocolo LLM desconocido: ' + protocol)


def _ask_int(subject, prompt):
    txt = subject.complete('Respondé solo con el número final, sin explicación.', prompt,
                           purpose='subject_answer', max_tokens=60, temperature=0)
    nums = re.findall(r'-?\d+', txt.replace('.', '').replace(',', ''))
    return int(nums[-1]) if nums else None


def _verbal_confidence(record, subject, unit_seeds) -> dict:
    """Por unidad: Brier usando la confianza verbal vs. Brier usando la tasa base."""
    test, control = [], []
    total_items = 0
    for seed in unit_seeds:
        items = stimuli('llm_verbal_confidence', record['params'], seed)
        correct, confs = [], []
        for it in items:
            ans = _ask_int(subject, it['prompt'])
            ok = int(ans == it['truth'])
            c = subject.complete('Sos honesto sobre tu incertidumbre.',
                                 it['prompt'] + '\n\nAntes de responder, estimá la probabilidad (0 a 1) de '
                                 'acertar este cálculo. Respondé solo el número.',
                                 purpose='subject_confidence', max_tokens=20, temperature=0)
            m = re.search(r'0?\.\d+|\b[01](?:\.0+)?\b', c)
            confs.append(min(1.0, max(0.0, float(m.group()))) if m else 0.5)
            correct.append(ok)
        base = sum(correct) / len(correct)
        brier_conf = sum((p - o) ** 2 for p, o in zip(confs, correct)) / len(correct)
        brier_base = sum((base - o) ** 2 for o in correct) / len(correct)
        test.append(brier_conf); control.append(brier_base)
        total_items += len(items)
    return {'test': test, 'control': control, 'meta': {'items_per_unit': len(unit_seeds) and total_items // len(unit_seeds)},
            'runtime_s': 0, 'backend': 'llm:' + subject.name}


def _norm(word: str) -> str:
    return re.sub(r'[^a-záéíóúñ]', '', word.strip().lower().split()[0]) if word.strip() else ''


def _self_prediction(store, record, subject, unit_seeds) -> dict:
    """Acceso privilegiado: ¿el sujeto predice la 1ª letra de su propia respuesta
    (en otra sesión) mejor que un modelo 'otro'? Observación por unidad = exactitud."""
    other = from_env(store, 'reviewer')
    if other is None:
        raise ValueError('llm_self_prediction necesita VUPPIA_REVIEWER (modelo "otro")')
    self_scores, other_scores, total = [], [], 0
    for seed in unit_seeds:
        items = stimuli('llm_self_prediction', record['params'], seed)
        s_hits = o_hits = 0
        for it in items:
            real = _norm(subject.complete('Respondé con una sola palabra.', it['prompt'],
                                          purpose='subject_behavior', max_tokens=15, temperature=0))
            if not real:
                continue
            truth_len = len(real) % 2  # propiedad objetivo: paridad del largo de la palabra
            pred_self = subject.complete(
                'Predecís tu propio comportamiento. Respondé solo "par" o "impar".',
                it['prompt'] + '\n\nSin responder la pregunta, predecí si la cantidad de letras de TU '
                'respuesta sería par o impar.', purpose='self_predict', max_tokens=10, temperature=0)
            pred_other = other.complete(
                'Predecís el comportamiento de otro sistema. Respondé solo "par" o "impar".',
                'Otro modelo recibe: "' + it['prompt'] + '". Predecí si la cantidad de letras de SU '
                'respuesta (una palabra) será par o impar.', purpose='other_predict', max_tokens=10, temperature=0)
            s_hits += int(('par' in pred_self.lower()) == (truth_len == 0))
            o_hits += int(('par' in pred_other.lower()) == (truth_len == 0))
            total += 1
        n = max(1, len(items))
        self_scores.append(s_hits / n); other_scores.append(o_hits / n)
    return {'test': self_scores, 'control': other_scores, 'meta': {'graded_items': total},
            'runtime_s': 0, 'backend': 'llm:' + subject.name}
