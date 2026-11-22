"""Protocolo integrado (simulacion): ¿mas datos de entrenamiento mejoran la generalizacion?

Curva de aprendizaje: se entrena la MISMA arquitectura con pocos vs muchos datos.
Pregunta: ¿la version con mas datos logra mayor exactitud en datos no vistos?
Resultado no fijado por construccion. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP
import numpy as np

ARMS = ['small', 'large', 'small_b']


def _acc(task, n, p, rng, xte, yte):
    xtr, ytr = sample(task, rng, int(n), noise=p['noise'])
    nr = np.random.default_rng(rng.integers(1 << 62))
    net = MLP(task['d'], int(p['hidden']), nr).fit(xtr, ytr, epochs=int(p['epochs']), rng=nr)
    return float((net.forward(xte)[1].argmax(1) == yte).mean())


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xte, yte = sample(task, rng, 2000, noise=p['noise'])
        if 'small' in out:
            out['small'].append(_acc(task, p['n_small'], p, rng, xte, yte))
        if 'large' in out:
            out['large'].append(_acc(task, p['n_large'], p, rng, xte, yte))
        if 'small_b' in out:
            out['small_b'].append(_acc(task, p['n_small'], p, rng, xte, yte))
    return {'arms': out, 'meta': {}}
