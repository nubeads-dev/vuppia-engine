"""Protocolo integrado (simulacion): CABEZA METACOGNITIVA entrenada en conjunto.

Mecanismo arquitectonico NUEVO (no un probe post-hoc ni un ajuste de scoring):
una red con una cabeza auxiliar que, ENTRENADA JUNTO con la tarea principal,
aprende a predecir su propio error. La representacion oculta queda moldeada para
soportar metacognicion. Pregunta falsable: esa cabeza metacognitiva, ?detecta sus
propios errores MEJOR que la confianza softmax de una red estandar (maxprob)?

Medida: AUROC al separar aciertos de errores en datos no vistos (mayor es mejor).
A/A: maxprob vs maxprob_b (misma red, dos muestras) debe dar ~0. No mide
experiencia subjetiva; mide una capacidad funcional de autoevaluacion.
"""
from substrate import make_task, sample, MLP, softmax, auroc
import numpy as np

ARMS = ['metacog', 'maxprob', 'maxprob_b']


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class MetaMLP:
    """MLP con cabeza principal (2 logits) + cabeza metacognitiva (1 logit -> P(error)),
    con capa oculta compartida y entrenamiento conjunto. Backprop manual (solo numpy)."""

    def __init__(self, d, h, rng):
        self.W1 = rng.normal(0, 1 / np.sqrt(d), (d, h)); self.b1 = np.zeros(h)
        self.W2 = rng.normal(0, 1 / np.sqrt(h), (h, 2)); self.b2 = np.zeros(2)
        self.Wm = rng.normal(0, 1 / np.sqrt(h), (h, 1)); self.bm = np.zeros(1)

    def forward(self, x):
        hdn = np.tanh(x @ self.W1 + self.b1)
        logits = hdn @ self.W2 + self.b2
        mlog = hdn @ self.Wm + self.bm
        return hdn, logits, mlog

    def fit(self, x, y, epochs=60, lr=0.02, lam=0.5, rng=None, batch=128):
        params = [self.W1, self.b1, self.W2, self.b2, self.Wm, self.bm]
        m = [np.zeros_like(p) for p in params]; v = [np.zeros_like(p) for p in params]
        n = len(x); t = 0
        for _ in range(epochs):
            order = rng.permutation(n) if rng is not None else np.arange(n)
            for s in range(0, n, batch):
                idx = order[s:s + batch]
                xb, yb = x[idx], y[idx]
                hdn, logits, mlog = self.forward(xb)
                nb = len(yb)
                # Cabeza principal (cross-entropy).
                p = softmax(logits)
                g = p.copy(); g[np.arange(nb), yb] -= 1; g /= nb
                gW2 = hdn.T @ g; gb2 = g.sum(0)
                gh_main = g @ self.W2.T
                # Cabeza metacognitiva: objetivo = se equivoco en ESTE ejemplo (detach del argmax).
                target = (logits.argmax(1) != yb).astype(float).reshape(-1, 1)
                pm = _sigmoid(mlog)
                gm = lam * (pm - target) / nb
                gWm = hdn.T @ gm; gbm = gm.sum(0)
                gh_meta = gm @ self.Wm.T
                # Capa oculta compartida (recibe gradiente de ambas cabezas).
                gh = (gh_main + gh_meta) * (1 - hdn ** 2)
                gW1 = xb.T @ gh; gb1 = gh.sum(0)
                t += 1
                for i, gr in enumerate([gW1, gb1, gW2, gb2, gWm, gbm]):
                    m[i] = 0.9 * m[i] + 0.1 * gr
                    v[i] = 0.999 * v[i] + 0.001 * gr * gr
                    params[i] -= lr * (m[i] / (1 - 0.9 ** t)) / (np.sqrt(v[i] / (1 - 0.999 ** t)) + 1e-8)
        return self


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    d = 10
    for seed in args['unit_seeds']:
        rng = np.random.default_rng(seed)
        task = make_task(rng, d)
        n_train = int(p['n_train']); hidden = int(p['hidden'])
        epochs = int(p['epochs']); noise = p['noise']; lam = p.get('lam', 0.5)
        xtr, ytr = sample(task, rng, n_train, noise=noise)
        xte, yte = sample(task, rng, 1500, noise=noise)
        xte2, yte2 = sample(task, rng, 1500, noise=noise)

        # Baseline: red estandar; score de error = 1 - maxprob (su propia confianza).
        base = MLP(d, hidden, np.random.default_rng(rng.integers(1 << 62))).fit(
            xtr, ytr, epochs=epochs, rng=rng)
        _, lg_te = base.forward(xte); _, lg_te2 = base.forward(xte2)
        err_base = (lg_te.argmax(1) != yte).astype(int)
        err_base2 = (lg_te2.argmax(1) != yte2).astype(int)
        score_mp = 1 - softmax(lg_te).max(1)
        score_mp2 = 1 - softmax(lg_te2).max(1)

        # Mecanismo nuevo: red con cabeza metacognitiva entrenada en conjunto.
        meta = MetaMLP(d, hidden, np.random.default_rng(rng.integers(1 << 62))).fit(
            xtr, ytr, epochs=epochs, lam=lam, rng=rng)
        _, lg_m, mlog_m = meta.forward(xte)
        err_meta = (lg_m.argmax(1) != yte).astype(int)
        score_meta = _sigmoid(mlog_m).reshape(-1)   # P(error) aprendida

        if 'metacog' in out:
            out['metacog'].append(auroc(score_meta, err_meta))
        if 'maxprob' in out:
            out['maxprob'].append(auroc(score_mp, err_base))
        if 'maxprob_b' in out:
            out['maxprob_b'].append(auroc(score_mp2, err_base2))
    return {'arms': out, 'meta': {'lam': float(p.get('lam', 0.5))}}
