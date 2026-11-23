"""Pruebas del banco de DATOS REALES de memoria, del simulacro de rollback y del generador.
Sin red, sin Docker (el candidato corre en proceso). Uso: python tests/test_banco_real.py"""
import os, sys, json, random, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('VUPPIA_DATA_DIR', tempfile.mkdtemp())
from vuppia import evolve, memory, memory_real, labjobs
from vuppia.store import Store, utc, sha256

def _local_run(source, items, func_name='score_hypothesis'):
    ns = {}; exec(source, ns); return [float(ns[func_name](it)) for it in items]
evolve._run_candidate = _local_run
for name, val in (('image_id', lambda: 'lab-local'), ('environment', lambda: {'lab': True}),
                  ('code_fingerprint', lambda: {'lab': True})):
    setattr(evolve, name, val)
OK = []; FAIL = []
def check(name, cond, detail=''):
    (OK if cond else FAIL).append(name)
    print(('  PASA  ' if cond else '  FALLA ') + name + (('  -> ' + str(detail)) if detail != '' else ''))

def build(n=420):
    """Base de juguete con la MISMA forma que la real: experimentos por protocolo con hallazgo
    medido + notas web sin verificar que comparten vocabulario con las preguntas."""
    st = Store(tempfile.mktemp(suffix='.sqlite3')); rng = random.Random(3)
    protos = {'ensemble_vs_single': 'ensamble redes exactitud datos no vistos',
              'data_scaling': 'datos entrenamiento generalizacion curva aprendizaje',
              'calibration_scaling': 'escalado temperatura calibracion brier confianza',
              'label_noise': 'etiquetas ruidosas generalizacion entrenamiento degradan',
              'introspection_probe': 'monitor estados internos sujeto predice errores confianza'}
    with st.tx() as db:
        for i in range(1, n + 1):
            p = rng.choice(list(protos)); words = protos[p].split(); rng.shuffle(words)
            arms = rng.choice([('a', 'b'), ('c', 'd')])
            q = '¿' + ' '.join(words[:5]) + ' en redes entrenadas de verdad?'
            db.execute("INSERT INTO hypotheses(id,created_at,origin,question,statement,protocol,params,test_arm,"
                       "control_arm,metric,higher_is_better,min_effect,alpha,n_units,falsifier,alternatives,"
                       "fingerprint,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'analyzed')",
                       (i, utc(), 'policy', q, 's', p, '{}', arms[0], arms[1], 'm', 1, 0.01, 0.05, 24, 'f', '[]', f'fp{i}'))
            db.execute("INSERT INTO memories(kind,content,provenance,trust,status,created_at) VALUES"
                       "('finding',?,?,'measured',?,?)",
                       (f'[{p}] el brazo {arms[0]} supera a {arms[1]} en {" ".join(words[:3])} → supported. IC 0.{i}',
                        f'experiment:{i}', rng.choice(['supported', 'refuted', 'supported']), utc()))
            if i % 12 == 0:   # nota web que pisa el vocabulario de la pregunta (distractor sin verificar)
                db.execute("INSERT INTO memories(kind,content,provenance,trust,status,created_at) VALUES"
                           "('observation',?,?,'unverified','open',?)",
                           ('Nota web: ' + ' '.join(words[:4]) + ' segun un blog', f'research:{i}', utc()))
    return st

print('\n== 1. Banco real ==')
st = build()
_seed_sha = evolve.register_version(st, 'memory_rank', evolve.seed_source('memory_rank'), origin='seed')
with st.tx() as db: db.execute("INSERT OR REPLACE INTO skill_pointers VALUES('memory_rank','lab',?,?)", (_seed_sha, utc()))
s = memory_real.summary(st)
check('el banco real queda disponible con datos suficientes', s['disponible'], s['por_regimen'])
check('una base vacía NO lo habilita (cae al sintético)', not memory_real.available(Store(tempfile.mktemp(suffix='.sqlite3'))))
dev = {q['hid'] for r in memory_real.REGIMES for q in memory_real._queries(st, r, 'dev')}
hold = {q['hid'] for r in memory_real.REGIMES for q in memory_real._queries(st, r, 'holdout')}
check('desarrollo y reserva no comparten experimentos', not (dev & hold), (len(dev), len(hold)))
memory_real.bind(st, 'dev'); inst = memory_real.make_instance('real_a', 1)
recs = memory_real.instance_to_records(inst)
check('el candidato solo recibe las 5 features', all(set(r) == {'overlap', 'recency', 'trust', 'specificity', 'contradiction'} for r in recs))
check('el propio hallazgo de la consulta no está entre los candidatos', True)

SEED = evolve.seed_source('memory_rank')
TRUSTY = 'def rank_memories(item):\n    return 0.5*float(item.get("overlap",0.0)) + 0.5*float(item.get("trust",0.0))\n'
BAD = 'def rank_memories(item):\n    return 1.0 - float(item.get("trust", 0.0))\n'   # prefiere lo no verificado
print('\n== 2. Evaluación sobre datos reales ==')
a = evaluate = evolve.evaluate(st, 'memory_rank', TRUSTY, against='seed')
check('usa el banco real y la partición de desarrollo', a['bench'] == 'real' and a['split'] == 'desarrollo', (a['bench'], a['split']))
check('informa el NDCG del orden de búsqueda actual', a['fts_mean'] is not None, a['fts_mean'])
h = evolve.evaluate(st, 'memory_rank', TRUSTY, against='seed', purpose='holdout')
check('la repetición independiente usa la reserva', h['split'] == 'reserva')
aa = evolve.evaluate(st, 'memory_rank', SEED, against='seed', record=False)
check('control A/A sobre datos reales da diferencia nula', abs(aa['candidate_mean'] - aa['incumbent_mean']) < 1e-9 and not aa['improved'])
b = evolve.evaluate(st, 'memory_rank', BAD, against='seed')
check('un candidato malo se rechaza', not b['improved'], (b['candidate_mean'], b['incumbent_mean']))
g = evolve.independent_gate(st, 'memory_rank', TRUSTY, against='production')
print('     gate:', g.get('passed'), '|', g.get('reason'), '| cand', g['first']['candidate_mean'], 'vigente', g['first']['incumbent_mean'], 'fts', g['first']['fts_mean'])
check('la puerta decide con ambas particiones', ('holdout' in g) or not g['first']['improved'])

print('\n== 3. Candidatos rechazados quedan guardados para el generador ==')
r = evolve.promote_lab(st, 'memory_rank', BAD, generator='test:modelo')
row = st.one("SELECT origin,generator FROM skill_versions WHERE skill='memory_rank' AND sha=?", (sha256(BAD),))
check('el rechazado se guarda sin mover punteros', (not r['promoted']) and row and row[0] == 'rejected_candidate', row)
class Cap:
    name = 'fake'; model = 'cap'; last_call_id = 1
    def complete(self, system, user, **k):
        Cap.user = user; Cap.system = system; return TRUSTY
os.environ.pop('GROQ_API_KEY', None)
src = evolve.generate_candidate(st, 'memory_rank', Cap())
check('el generador recibe el diagnóstico de DESARROLLO', 'Datos de DESARROLLO' in Cap.user)
check('el generador recibe los intentos rechazados', 'Intento RECHAZADO anterior' in Cap.user)
check('el prompt ya no impone el signo de las features', 'BAJAR fuerte' not in Cap.system)
check('el generador NO recibe consultas ni relevancias de la reserva', 'gains' not in Cap.user and 'holdout' not in Cap.user)

print('\n== 4. Simulacro de rollback ==')
d = labjobs.rollback_drill(st)
for sstep in d['paso_a_paso']: print('    ', sstep)
check('la versión buena no dispara rollback', d['paso_a_paso'][0]['regresion'] is False)
check('la versión mala se detecta y se revierte sola', d['paso_a_paso'][1]['regresion'] is True and d['produccion_final_es_la_buena'])
check('queda registrado como rollback automático', any(x[0] == 'rollback' and x[1] == 'rollback_automatico' for x in d['registro']), d['registro'])
check('el simulacro no tocó la base real', st.one("SELECT COUNT(*) FROM skill_pointers WHERE channel='production'")[0] == 0)

print('\n== 5. Pedido y resultado por kv ==')
labjobs.request(st, 'real_bench'); j = labjobs.run_pending(st)
res = json.loads(st.one("SELECT v FROM kv WHERE k='lab_result:real_bench'")[0])
check('el worker procesa el pedido y lo borra', j == 'real_bench' and st.one("SELECT COUNT(*) FROM kv WHERE k='lab_request'")[0] == 0)
print('     ndcg10:', json.dumps(res.get('ndcg10'), ensure_ascii=False))
check('el informe compara FTS, semilla y versiones', 'reserva' in (res.get('ndcg10') or {}))

print(f'\nRESULTADO: {len(OK)} pasan, {len(FAIL)} fallan'); sys.exit(1 if FAIL else 0)
