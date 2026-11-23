"""A/B reproducible: version promovida vs semilla, con el mismo juez de VUPPIA (benchmarks +
grade), ejecutando las funciones directamente (sin sandbox). Semillas fijas -> mismo resultado."""
import sys, json, random, statistics as S
sys.path.insert(0, '.')
from vuppia import evolve
from vuppia.store import sha256

def load(src, fn):
    ns = {}; exec(src, ns); return ns[fn]

def run(skill, new_path, K=40):
    spec = evolve.skill_spec(skill); fn = spec['func']
    old_src = evolve.seed_source(skill); new_src = open(new_path, encoding='utf-8').read()
    f_old, f_new = load(old_src, fn), load(new_src, fn)
    out = {'skill': skill, 'old_sha': sha256(old_src)[:12], 'new_sha': sha256(new_src)[:12], 'regimes': []}
    deltas_all = []
    regimes = list(spec['regimes'])
    for reg in regimes:
        d = []; o = []; n = []
        for k in range(K):
            seed = int(sha256(f'audit-2026-11-23:{skill}:{reg}:{k}')[:12], 16)
            inst = spec['make'](reg, seed); recs = spec['records'](inst)
            so = spec['grade']([f_old(r) for r in recs], inst)['score']
            sn = spec['grade']([f_new(r) for r in recs], inst)['score']
            o.append(so); n.append(sn); d.append(sn - so)
        deltas_all += d
        out['regimes'].append({'regime': reg, 'old': round(S.mean(o), 4), 'new': round(S.mean(n), 4),
                               'delta': round(S.mean(d), 4), 'wins': sum(x > 0 for x in d),
                               'losses': sum(x < 0 for x in d), 'n': K})
    rng = random.Random(1); boots = sorted(S.mean(rng.choices(deltas_all, k=len(deltas_all))) for _ in range(4000))
    out['overall'] = {'delta': round(S.mean(deltas_all), 4), 'ci95': [round(boots[100], 4), round(boots[3899], 4)],
                      'wins': sum(x > 0 for x in deltas_all), 'losses': sum(x < 0 for x in deltas_all),
                      'units': len(deltas_all)}
    return out

if __name__ == '__main__':
    res = [run('memory_rank', 'evolved_export/memory_rank.lab.2750a742c202.py'),
           run('hypothesis', 'evolved_export/hypothesis.lab.6dcba4ea48b3.py')]
    print(json.dumps(res, indent=1))
