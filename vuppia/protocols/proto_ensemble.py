"""Protocolo integrado (simulacion): ¿un ensamble predice mejor que una sola red?

Pregunta genuina de aprendizaje automatico: promediar varias redes entrenadas
de verdad, ¿mejora la exactitud en datos no vistos frente a una sola red? El
resultado NO esta fijado por construccion: depende del ruido, la capacidad y los
datos. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP, softmax
import numpy as np

ARMS = ['single', 'ensemble', 'single_b']


def _acc(probs, y):
    return float((probs.argmax(1) == y).mean())


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xtr, ytr = sample(task, rng, int(p['n_train']), noise=p['noise'])
        xte, yte = sample(task, rng, 2000, noise=p['noise'])
        probs = []
        for _ in range(int(p['members'])):
            nr = np.random.default_rng(rng.integers(1 << 62))
            net = MLP(task['d'], int(p['hidden']), nr).fit(xtr, ytr, epochs=int(p['epochs']), rng=nr)
            probs.append(softmax(net.forward(xte)[1]))
        ens = np.mean(probs, axis=0)
        if 'single' in out:
            out['single'].append(_acc(probs[0], yte))
        if 'single_b' in out:
            out['single_b'].append(_acc(probs[1 % len(probs)], yte))
        if 'ensemble' in out:
            out['ensemble'].append(_acc(ens, yte))
    return {'arms': out, 'meta': {'members': int(p['members'])}}
