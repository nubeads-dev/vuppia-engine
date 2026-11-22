"""Runner CONFIABLE que acompaña al candidato dentro de su contenedor aislado.

Recibe SOLO vectores de features (sin etiquetas, sin verdad, sin semilla, sin
coeficientes). Importa el candidato, lo evalúa ítem por ítem y escribe los scores.
La calificación ocurre FUERA de este contenedor (en el host). Un candidato no puede
leer datos reservados porque no están en este contenedor.
"""
import importlib.util
import json
import math


def main(args):
    spec = importlib.util.spec_from_file_location('candidate', '/job/candidate.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    func_name = args.get('func', 'score_hypothesis')
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        return {'error': 'el candidato no define ' + func_name, 'scores': None}
    scores = []
    for item in args['items']:
        try:
            out = fn(dict(item))
        except Exception as e:                       # noqa: BLE001 - candidato no confiable
            return {'error': 'excepcion en el candidato: ' + type(e).__name__, 'scores': None}
        if not isinstance(out, (int, float, bool)) or isinstance(out, bool) or not math.isfinite(out):
            return {'error': 'salida no numerica/finita', 'scores': None}
        scores.append(float(out))
    return {'scores': scores, 'error': None}
