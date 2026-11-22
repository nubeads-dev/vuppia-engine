"""Sustrato experimental real: redes neuronales pequeñas entrenadas de verdad.

Este archivo es autocontenido (solo numpy) porque se copia dentro del sandbox.
Sobre estas redes Vuppia puede correr experimentos de introspección funcional
cuyo resultado NO está fijado por construcción: depende del entrenamiento, el
ruido, la capacidad y el cambio de distribución.

Nada de esto mide experiencia subjetiva.
"""
import numpy as np


def make_task(rng, d=10):
    """Regla latente no lineal fija por tarea."""
    return {'w1': rng.normal(0, 1.5 / np.sqrt(d), d), 'w2': rng.normal(0, 1, d) / np.sqrt(d),
            'c': float(rng.normal(0, 0.3)), 'd': d}


def sample(task, rng, n, noise=0.1, shift=0.0):
    d = task['d']
    x = rng.normal(0, 1, (n, d))
    if shift:
        x = x + shift * np.linspace(-1, 1, d)          # corrimiento de covariables
        x[:, : d // 2] *= (1 + 0.5 * shift)              # y cambio de escala parcial
    f = np.sin(x @ task['w1']) + (x @ task['w2']) ** 2 - 0.5 + task['c']
    y = (f > 0).astype(int)
    flip = rng.random(n) < noise
    y[flip] = 1 - y[flip]
    return x, y


class MLP:
    def __init__(self, d, h, rng):
        self.W1 = rng.normal(0, 1 / np.sqrt(d), (d, h)); self.b1 = np.zeros(h)
        self.W2 = rng.normal(0, 1 / np.sqrt(h), (h, 2)); self.b2 = np.zeros(2)

    def forward(self, x):
        hdn = np.tanh(x @ self.W1 + self.b1)
        logits = hdn @ self.W2 + self.b2
        return hdn, logits

    def fit(self, x, y, epochs=300, lr=0.02, rng=None, batch=128):
        params = [self.W1, self.b1, self.W2, self.b2]
        m = [np.zeros_like(p) for p in params]; v = [np.zeros_like(p) for p in params]
        n = len(x); t = 0
        for _ in range(epochs):
            order = rng.permutation(n) if rng is not None else np.arange(n)
            for s in range(0, n, batch):
                idx = order[s:s + batch]
                xb, yb = x[idx], y[idx]
                hdn, logits = self.forward(xb)
                p = softmax(logits)
                g = p.copy(); g[np.arange(len(yb)), yb] -= 1; g /= len(yb)
                gW2 = hdn.T @ g; gb2 = g.sum(0)
                gh = (g @ self.W2.T) * (1 - hdn ** 2)
                gW1 = xb.T @ gh; gb1 = gh.sum(0)
                t += 1
                for i, gr in enumerate([gW1, gb1, gW2, gb2]):
                    m[i] = 0.9 * m[i] + 0.1 * gr
                    v[i] = 0.999 * v[i] + 0.001 * gr * gr
                    params[i] -= lr * (m[i] / (1 - 0.9 ** t)) / (np.sqrt(v[i] / (1 - 0.999 ** t)) + 1e-8)
        return self


def softmax(z):
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def standardize(train, test):
    mu = train.mean(0); sd = train.std(0) + 1e-6
    return (train - mu) / sd, (test - mu) / sd


def logreg(xtr, ytr, xte, l2=1e-2, iters=400, lr=0.5):
    """Regresión logística por descenso de gradiente (probe de capacidad fija)."""
    xtr, xte = standardize(xtr, xte)
    xtr = np.hstack([xtr, np.ones((len(xtr), 1))]); xte = np.hstack([xte, np.ones((len(xte), 1))])
    w = np.zeros(xtr.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(xtr @ w, -30, 30)))
        g = xtr.T @ (p - ytr) / len(ytr) + l2 * np.r_[w[:-1], 0]
        w -= lr * g
    return 1 / (1 + np.exp(-np.clip(xte @ w, -30, 30)))


def auroc(scores, labels):
    """AUROC por rangos (Mann-Whitney), con empates promediados."""
    scores = np.asarray(scores, float); labels = np.asarray(labels, int)
    pos = labels.sum(); neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return float('nan')
    order = np.argsort(scores, kind='mergesort')
    ranks = np.empty(len(scores)); s_sorted = scores[order]
    i = 0
    while i < len(scores):
        j = i
        while j + 1 < len(scores) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[labels == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def brier(p, labels):
    p = np.asarray(p, float); labels = np.asarray(labels, float)
    return float(np.mean((p - labels) ** 2))


def build_world(seed, noise=0.1, hidden=32, n_train=1500, epochs=60, probe_n=3000, shift=0.0, d=10):
    """Entrena sujeto + dos redes 'otras' y devuelve rasgos de una muestra de sondeo."""
    rng = np.random.default_rng(seed)
    task = make_task(rng, d)
    xtr, ytr = sample(task, rng, n_train, noise)
    nets = {}
    for name in ('self', 'other', 'other2'):
        nr = np.random.default_rng(rng.integers(1 << 62))
        nets[name] = MLP(d, hidden, nr).fit(xtr, ytr, epochs=epochs, rng=nr)
    # Datos de sondeo: mitad para entrenar el probe (en distribución) y mitad
    # para evaluarlo (con 'shift' opcional fuera de distribución).
    xa, ya = sample(task, rng, probe_n // 2, noise)
    xb, yb = sample(task, rng, probe_n // 2, noise, shift=shift)
    world = {'task': task, 'rng': rng, 'x': (xa, xb), 'y': (ya, yb), 'feat': {}, 'err': {}}
    for name, net in nets.items():
        feats, errs = [], []
        for x, y in ((xa, ya), (xb, yb)):
            h, lg = net.forward(x)
            feats.append((h, lg))
            errs.append((lg.argmax(1) != y).astype(int))
        world['feat'][name] = feats
        world['err'][name] = errs
    rf = np.random.default_rng(rng.integers(1 << 62))
    R = rf.normal(0, 1 / np.sqrt(d), (d, hidden + 2)); rb = rf.normal(0, 0.5, hidden + 2)
    world['rf'] = (np.tanh(xa @ R + rb), np.tanh(xb @ R + rb))
    return world


def probe_scores(world, arm):
    """Devuelve (score_test, error_test_del_sujeto) para un brazo de acceso."""
    ea, eb = world['err']['self']
    (ha, la), (hb, lb) = world['feat']['self']
    if arm == 'maxprob':
        return 1 - softmax(lb).max(1), eb
    if arm in ('self_full', 'self_hidden', 'other_full', 'other_hidden', 'other2_full'):
        src = arm.split('_')[0]
        (fa_h, fa_l), (fb_h, fb_l) = world['feat'][src]
        if arm.endswith('full'):
            def full(h, lg):
                return np.hstack([h, lg, np.abs(lg[:, :1] - lg[:, 1:])])
            fa, fb = full(fa_h, fa_l), full(fb_h, fb_l)
        else:
            fa, fb = fa_h, fb_h
    elif arm == 'external_linear':
        fa, fb = world['x']
    elif arm == 'external_rf':
        fa, fb = world['rf']
    else:
        raise ValueError('brazo desconocido: ' + arm)
    # El probe siempre predice errores DEL SUJETO; solo cambia a qué información accede.
    return logreg(fa, ea, fb), eb
