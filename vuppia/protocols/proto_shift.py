"""Protocolo integrado (simulacion): ¿la red se degrada bajo cambio de distribucion?

Se entrena una red en distribucion y se mide su exactitud en datos propios vs en
datos con corrimiento de covariables (shift). Mide robustez funcional fuera de
distribucion. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP
import numpy as np

ARMS = ['shifted', 'in_dist', 'shifted_b']


def _acc(net, x, y):
    return float((net.forward(x)[1].argmax(1) == y).mean())


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xtr, ytr = sample(task, rng, int(p['n_train']), noise=p['noise'])
        nr = np.random.default_rng(rng.integers(1 << 62))
        net = MLP(task['d'], int(p['hidden']), nr).fit(xtr, ytr, epochs=int(p['epochs']), rng=nr)
        xin, yin = sample(task, rng, 2000, noise=p['noise'])
        xsh, ysh = sample(task, rng, 2000, noise=p['noise'], shift=p['shift'])
        xs2, ys2 = sample(task, rng, 2000, noise=p['noise'], shift=p['shift'])
        if 'in_dist' in out:
            out['in_dist'].append(_acc(net, xin, yin))
        if 'shifted' in out:
            out['shifted'].append(_acc(net, xsh, ysh))
        if 'shifted_b' in out:
            out['shifted_b'].append(_acc(net, xs2, ys2))
    return {'arms': out, 'meta': {}}
