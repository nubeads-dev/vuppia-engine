"""Protocolo integrado (simulacion): ¿la confianza de la red predice sus propios errores?

Metacognicion funcional: ¿la probabilidad maxima (confianza) de una red entrenada
de verdad predice en que casos se equivoca, mejor que un puntaje aleatorio?
Metrica AUROC. Resultado no fijado por construccion. No mide experiencia subjetiva.
"""
from substrate import make_task, sample, MLP, softmax, auroc
import numpy as np

ARMS = ['baseline', 'confidence', 'baseline_b']


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng)
        xtr, ytr = sample(task, rng, int(p['n_train']), noise=p['noise'])
        net = MLP(task['d'], int(p['hidden']), rng).fit(xtr, ytr, epochs=int(p['epochs']), rng=rng)
        xte, yte = sample(task, rng, 3000, noise=p['noise'])
        probs = softmax(net.forward(xte)[1])
        err = (probs.argmax(1) != yte).astype(int)
        if err.sum() == 0 or err.sum() == len(err):
            for a in arms:
                out[a].append(0.5)
            continue
        conf = 1 - probs.max(1)                      # score alto = menos confianza = mas error esperado
        if 'confidence' in out:
            out['confidence'].append(auroc(conf, err))
        if 'baseline' in out:
            out['baseline'].append(auroc(rng.random(len(err)), err))
        if 'baseline_b' in out:
            out['baseline_b'].append(auroc(rng.random(len(err)), err))
    return {'arms': out, 'meta': {}}
