"""Protocolo integrado (simulacion): ¿el ruido de etiquetas en el entrenamiento degrada la generalizacion?

Se entrena la MISMA arquitectura con etiquetas poco ruidosas (clean) vs muy ruidosas
(noisy) y se evalua en un test LIMPIO. Pregunta: ¿clean supera a noisy? Mide robustez
funcional al ruido de supervision. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP
import numpy as np

ARMS = ['noisy', 'clean', 'noisy_b']


def _run(task, train_noise, p, rng, xte, yte):
    xtr, ytr = sample(task, rng, int(p['n_train']), noise=train_noise)
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
        xte, yte = sample(task, rng, 2000, noise=0.0)
        if 'clean' in out:
            out['clean'].append(_run(task, p['clean_noise'], p, rng, xte, yte))
        if 'noisy' in out:
            out['noisy'].append(_run(task, p['noisy_noise'], p, rng, xte, yte))
        if 'noisy_b' in out:
            out['noisy_b'].append(_run(task, p['noisy_noise'], p, rng, xte, yte))
    return {'arms': out, 'meta': {}}
