"""Protocolo integrado (simulacion): ¿el escalado por temperatura mejora la calibracion?

Se entrena una red de verdad y se ajusta una temperatura T en un conjunto de
validacion. Pregunta: ¿las probabilidades escaladas tienen MENOR Brier (mejor
calibracion) que las crudas en datos no vistos? El resultado no esta fijado por
construccion. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP, softmax, brier
import numpy as np

ARMS = ['raw', 'temp_scaled', 'raw_b']


def _fit_T(logits, y):
    best, bestT = 1e9, 1.0
    yb = (np.asarray(y) == 1).astype(float)
    for T in np.linspace(0.5, 5.0, 19):
        p = softmax(logits / T)[:, 1]
        b = brier(p, yb)
        if b < best:
            best, bestT = b, float(T)
    return bestT


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xtr, ytr = sample(task, rng, int(p['n_train']), noise=p['noise'])
        net = MLP(task['d'], int(p['hidden']), rng).fit(xtr, ytr, epochs=int(p['epochs']), rng=rng)
        xval, yval = sample(task, rng, 1500, noise=p['noise'])
        xte, yte = sample(task, rng, 1500, noise=p['noise'])
        xte2, yte2 = sample(task, rng, 1500, noise=p['noise'])
        T = _fit_T(net.forward(xval)[1], yval)
        praw = softmax(net.forward(xte)[1])[:, 1]
        psc = softmax(net.forward(xte)[1] / T)[:, 1]
        praw2 = softmax(net.forward(xte2)[1])[:, 1]
        if 'raw' in out:
            out['raw'].append(brier(praw, (yte == 1).astype(float)))
        if 'temp_scaled' in out:
            out['temp_scaled'].append(brier(psc, (yte == 1).astype(float)))
        if 'raw_b' in out:
            out['raw_b'].append(brier(praw2, (yte2 == 1).astype(float)))
    return {'arms': out, 'meta': {}}
