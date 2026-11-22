"""Protocolo integrado: ¿acceso privilegiado a los propios errores?

Por unidad (semilla) se entrenan tres redes sobre la misma tarea: el SUJETO y dos
redes 'otras'. Probes de capacidad fija intentan predecir los errores del sujeto
con distinta información: activaciones propias, de otra red, el input crudo,
rasgos aleatorios no lineales, o la confianza de salida. Métrica: AUROC.
Se ejecuta dentro del sandbox. Resultado no fijado por construcción.
"""
from substrate import auroc, build_world, probe_scores

ARMS = ['maxprob', 'self_full', 'self_hidden', 'other_full', 'other_hidden', 'other2_full',
        'external_linear', 'external_rf']


def main(args):
    p = args['params']
    arms = args.get('arms') or ARMS
    out = {a: [] for a in arms}
    err_rates = []
    for seed in args['unit_seeds']:
        w = build_world(seed, noise=p['noise'], hidden=int(p['hidden']), n_train=int(p['n_train']),
                        epochs=int(p['epochs']), probe_n=int(p['probe_n']), shift=p['shift'])
        err_rates.append(float(w['err']['self'][1].mean()))
        for a in arms:
            s, e = probe_scores(w, a)
            out[a].append(auroc(s, e))
    return {'arms': out, 'meta': {'subject_error_rate': err_rates}}
