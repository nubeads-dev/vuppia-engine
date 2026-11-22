"""Protocolo integrado (simulacion): ¿una red mas ancha generaliza mejor?

Con datos fijos, se entrenan una red angosta y una ancha de verdad. Pregunta:
¿la ancha logra MAYOR exactitud en datos no vistos? Puede ir para cualquier lado
(mas capacidad ayuda, pero tambien puede sobreajustar con ruido). No fijado por
construccion. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP
import numpy as np

ARMS = ['narrow', 'wide', 'narrow_b']


def _train_acc(task, xtr, ytr, xte, yte, h, epochs, rng):
    nr = np.random.default_rng(rng.integers(1 << 62))
    net = MLP(task['d'], int(h), nr).fit(xtr, ytr, epochs=int(epochs), rng=nr)
    return float((net.forward(xte)[1].argmax(1) == yte).mean())


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xtr, ytr = sample(task, rng, int(p['n_train']), noise=p['noise'])
        xte, yte = sample(task, rng, 2000, noise=p['noise'])
        if 'narrow' in out:
            out['narrow'].append(_train_acc(task, xtr, ytr, xte, yte, p['narrow_h'], p['epochs'], rng))
        if 'wide' in out:
            out['wide'].append(_train_acc(task, xtr, ytr, xte, yte, p['wide_h'], p['epochs'], rng))
        if 'narrow_b' in out:
            out['narrow_b'].append(_train_acc(task, xtr, ytr, xte, yte, p['narrow_h'], p['epochs'], rng))
    return {'arms': out, 'meta': {}}
