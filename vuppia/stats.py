"""Analizador confiable. Nunca lo ejecuta código propuesto; corre en el host.

Recibe observaciones crudas pareadas (una por unidad y brazo) y aplica la regla
de decisión que quedó sellada en el preregistro. No conoce la hipótesis más allá
de qué brazo es 'test', cuál es 'control', el efecto mínimo y alfa.
"""
from __future__ import annotations

import math
import random
import statistics

VERDICTS = ('supported', 'refuted', 'inconclusive', 'trivial', 'invalid')


def paired_analysis(test: list[float], control: list[float], *, min_effect: float,
                    alpha: float = 0.05, higher_is_better: bool = True,
                    seed: int = 0, n_boot: int = 4000, check_triviality: bool = True) -> dict:
    if len(test) != len(control) or len(test) < 4:
        return {'verdict': 'invalid', 'reason': 'se necesitan >=4 unidades pareadas'}
    if any(not (isinstance(v, (int, float)) and math.isfinite(v)) for v in test + control):
        return {'verdict': 'invalid', 'reason': 'observaciones no finitas'}
    sign = 1.0 if higher_is_better else -1.0
    diffs = [sign * (t - c) for t, c in zip(test, control)]
    n = len(diffs)
    mean = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    rng = random.Random(seed)
    boots = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(n_boot))
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[int((1 - alpha / 2) * n_boot) - 1]
    # Test de permutación por cambio de signo (exacto si n<=16).
    obs = abs(mean)
    if n <= 16:
        count = total = 0
        for mask in range(1 << n):
            m = statistics.fmean(d if (mask >> i) & 1 else -d for i, d in enumerate(diffs))
            count += abs(m) >= obs - 1e-12; total += 1
        p = count / total
    else:
        count = sum(abs(statistics.fmean(d if rng.random() < .5 else -d for d in diffs)) >= obs - 1e-12
                    for _ in range(20000))
        p = (count + 1) / 20001
    flags = triviality_flags(test, control, diffs) if check_triviality else []
    if flags:
        verdict = 'trivial'
    elif lo > min_effect and p < alpha:
        verdict = 'supported'
    elif lo > -min_effect and hi < min_effect:
        verdict = 'trivial'         # IC entero dentro de la banda trivial [-min_effect, min_effect]:
        #                             significativo pero demasiado chico para importar (así el A/A da nulo)
    elif hi < min_effect:
        verdict = 'refuted'         # el efecto mínimo relevante quedó excluido (efecto negativo real)
    else:
        verdict = 'inconclusive'
    return {'verdict': verdict, 'n_units': n, 'mean_diff': round(mean, 6), 'sd_diff': round(sd, 6),
            'ci': [round(lo, 6), round(hi, 6)], 'p_value': round(p, 6),
            'effect_size_dz': round(mean / sd, 4) if sd > 0 else None,
            'min_effect': min_effect, 'alpha': alpha, 'flags': flags,
            'test_mean': round(statistics.fmean(test), 6), 'control_mean': round(statistics.fmean(control), 6)}


def triviality_flags(test, control, diffs) -> list[str]:
    """Detecta resultados fijados por construcción (como 2 de 3 estudios de v2.2)."""
    flags = []
    if statistics.pstdev(diffs) < 1e-12:
        flags.append('diferencia_idéntica_en_todas_las_unidades')
    if statistics.pstdev(test) < 1e-12 and statistics.pstdev(control) < 1e-12:
        flags.append('ambos_brazos_sin_varianza')
    if statistics.pstdev(test) < 1e-12 and max(test) in (0.0, 1.0):
        flags.append('brazo_test_en_techo_o_piso_constante')
    return flags


def required_units(sd_guess: float, min_effect: float, alpha=0.05, power=0.8) -> int:
    """Aproximación normal para test pareado unilateral-equivalente."""
    if min_effect <= 0 or sd_guess <= 0:
        return 8
    z_a = 1.959964 if alpha == 0.05 else _z(1 - alpha / 2)
    z_b = 0.841621 if power == 0.8 else _z(power)
    return max(8, math.ceil(((z_a + z_b) * sd_guess / min_effect) ** 2))


def _z(q):  # inversa normal por bisección (suficiente aquí)
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def brier(p: float, outcome: int) -> float:
    return (p - outcome) ** 2
