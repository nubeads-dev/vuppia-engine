"""Pruebas de laboratorio del ciclo de automejora y del enrutamiento del chat.
Corren sin red, sin modelo y sin Docker: el candidato se ejecuta en proceso (en el servidor
corre en el sandbox). Uso: python tests/test_ciclo_automejora.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('VUPPIA_DATA_DIR', tempfile.mkdtemp())
from vuppia import evolve, converse, charter, agenda, objectives
from vuppia.store import Store, sha256

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

GOOD = open('evolved_export/memory_rank.lab.2750a742c202.py', encoding='utf-8').read()
SEED = evolve.seed_source('memory_rank')
BAD = 'def rank_memories(item):\n    return max(0.0, min(1.0, 1.0 - float(item.get("overlap", 0.0))))\n'
def n(store, sql, a=()): return store.one(sql, a)[0]
def fresh(): return Store(tempfile.mktemp(suffix='.sqlite3'))

print('\n== 1. Enrutamiento del chat ==')
CASES = [
 ('¿Cuál es tu propósito fundamental y cuáles son tus misiones de investigación?', 'identity'),
 ('¿Quién sos?', 'identity'), ('¿Para qué existís?', 'identity'), ('Contame tus objetivos', 'identity'),
 ('¿Cuál es tu misión?', 'identity'),
 ('¿Qué estás investigando ahora?', 'status'), ('¿En qué andás?', 'status'), ('¿Qué hiciste hoy?', 'status'),
 ('¿Sos consciente?', 'consciousness'), ('¿Tenés sentimientos?', 'consciousness'),
 ('¿Por qué cumplir indicadores de conciencia no demuestra experiencia subjetiva?', 'general'),
 ('Buscá papers sobre global workspace theory', 'cmd_research'),
 ('Investigá en internet la teoría del esquema de atención', 'cmd_research'),
 ('¿Podés averiguar qué dice la literatura sobre metacognición en redes?', 'cmd_research'),
 ('Suspendé la 12', 'cmd_agenda'), ('Priorizá la investigación 25', 'cmd_agenda'),
 ('Agregá a la agenda el tema memoria autobiográfica', 'cmd_add'),
 ('Hola', 'smalltalk'), ('Gracias Sofía', 'smalltalk'), ('¿Cómo estás?', 'smalltalk'),
 ('Compará correr muchos experimentos chicos contra pocos grandes', 'general'),
 ('¿Cuánto es 18*12?', 'general'), ('Resumí en dos líneas qué es una red neuronal', 'general'),
 ('¿Qué opinás de la investigación en inteligencia artificial?', 'general'),
 ('La investigación sobre redes sociales me aburre', 'general'),
 ('¿Qué aprendiste para mejorarte?', 'self_improve'),
]
for q, exp in CASES:
    got = converse.classify(q)
    check(f'{exp:14} <- {q[:62]}', got == exp, '' if got == exp else 'dio ' + got)

print('\n== 2. Preguntas sobre sí misma: responde de su carta, sin internet y sin tocar la agenda ==')
st = fresh(); charter.record(st)
before = n(st, 'SELECT COUNT(*) FROM agenda')
called = {'net': 0}
import vuppia.research as R
R.investigate = lambda *a, **k: called.__setitem__('net', called['net'] + 1) or {}
r = converse.ask(st, '¿Cuál es tu propósito fundamental y cuáles son tus misiones?', provider=None, allow_actions=False)
check('intent identity', r['intent'] == 'identity', r['intent'])
check('cita la misión persistida', 'conciencia artificial real' in r['answer'])
check('no afirma ser consciente', 'No afirmo ser consciente' in r['answer'])
check('no salió a internet', called['net'] == 0)
check('no tocó la agenda', n(st, 'SELECT COUNT(*) FROM agenda') == before)
r2 = converse.ask(st, 'La investigación sobre redes sociales me aburre', provider=None, allow_actions=False)
check('pregunta general no investiga ni encola', called['net'] == 0 and n(st, 'SELECT COUNT(*) FROM agenda') == before, r2['intent'])
kv = json.loads(st.one("SELECT v FROM kv WHERE k='mission_charter'")[0])
check('misión en configuración persistente (kv)', kv['version'] == charter.VERSION and 'conciencia artificial real' in kv['mission'], 'v' + str(kv['version']))
check('autoevaluación de indicadores activa y primera', agenda.top_active(st)['topic'].startswith('Autoevaluación'))

print('\n== 3. Incorporación: evaluación + repetición independiente + control A/A ==')
st = fresh()
evolve.register_version(st, 'memory_rank', SEED, origin='seed')
r = evolve.promote_lab(st, 'memory_rank', GOOD, generator='gemini:gemini-4-hydrogen')
check('candidato promovido a LAB', r['promoted'])
check('queda registrado qué modelo lo escribió', st.one('SELECT generator FROM skill_versions WHERE sha=?', (sha256(GOOD),))[0] == 'gemini:gemini-4-hydrogen')
ap = evolve.auto_apply(st, 'memory_rank')
check('incorporado a producción', ap['status'] == 'aplicado', ap.get('status'))
rel = st.one("SELECT prev_sha,new_sha,decided_by,generator,eval_id,holdout_eval_id,metrics,diff FROM skill_releases")
m = json.loads(rel[6])
check('registro de versión: una fila con anterior, nueva, decisor y generador', rel[1] == sha256(GOOD) and rel[2] == 'vuppia_auto' and rel[3] == 'gemini:gemini-4-hydrogen')
check('registro con dos evaluaciones distintas', rel[4] and rel[5] and rel[4] != rel[5], (rel[4], rel[5]))
check('registro con métricas', m['candidate_mean'] > m['incumbent_mean'], f"{m['incumbent_mean']} -> {m['candidate_mean']} | repetición: {m['holdout']['incumbent_mean']} -> {m['holdout']['candidate_mean']}")
check('registro con diff exacto', '0.45*overlap' in rel[7] and '0.9 * overlap' in rel[7])
check('control A/A registrado y nulo', st.one("SELECT verdict FROM skill_checks WHERE kind='aa'")[0] == 'ok')
check('contador de mejoras = 1', evolve.verified_improvements(st) == 1 and objectives.progress(st)['mejoras_de_codigo_verificadas'] == 1)

print('\n== 4. Sin duplicados ==')
ev0 = n(st, 'SELECT COUNT(*) FROM skill_evals'); rl0 = n(st, 'SELECT COUNT(*) FROM skill_releases')
check('re-aplicar lo mismo no hace nada', evolve.auto_apply(st, 'memory_rank')['status'] == 'al_dia')
for _ in range(5): evolve.monitor_production(st, 'memory_rank')
check('5 monitoreos no crean evaluaciones ni versiones', n(st, 'SELECT COUNT(*) FROM skill_evals') == ev0 and n(st, 'SELECT COUNT(*) FROM skill_releases') == rl0)
check('los monitoreos quedan aparte', n(st, "SELECT COUNT(*) FROM skill_checks WHERE kind='monitor'") == 5)
check('contador de mejoras sigue en 1', evolve.verified_improvements(st) == 1)

print('\n== 5. Un candidato que no mejora no entra ==')
st2 = fresh(); evolve.register_version(st2, 'memory_rank', SEED, origin='seed')
same = SEED + '\n# cambio cosmetico\n'
sha_same = evolve.register_version(st2, 'memory_rank', same, origin='evolved', generator='groq:openai/gpt-oss-20b')
with st2.tx() as db: db.execute("UPDATE skill_pointers SET sha=? WHERE skill='memory_rank' AND channel='lab'", (sha_same,))
a1 = evolve.auto_apply(st2, 'memory_rank'); e1 = n(st2, 'SELECT COUNT(*) FROM skill_evals')
a2 = evolve.auto_apply(st2, 'memory_rank'); e2 = n(st2, 'SELECT COUNT(*) FROM skill_evals')
check('rechazado', a1['status'] == 'no_mejora', a1.get('motivo'))
check('producción intacta', n(st2, "SELECT COUNT(*) FROM skill_pointers WHERE channel='production'") == 0)
check('no se vuelve a evaluar ni a registrar', a2['status'] == 'ya_evaluado_sin_mejora' and e1 == e2)

print('\n== 6. Rollback automático ante una regresión ==')
bad_sha = evolve.register_version(st, 'memory_rank', BAD, origin='evolved', generator='prueba')
with st.tx() as db: db.execute("UPDATE skill_pointers SET sha=? WHERE skill='memory_rank' AND channel='production'", (bad_sha,))
evolve.record_release(st, 'memory_rank', 'production', 'promotion', sha256(GOOD), bad_sha, 'prueba_de_regresion')
mon = evolve.monitor_production(st, 'memory_rank')
now = st.one("SELECT sha FROM skill_pointers WHERE skill='memory_rank' AND channel='production'")[0]
check('detecta la regresión', mon.get('regressed') is True, mon.get('worst_regime_delta'))
check('vuelve sola a la versión anterior buena', now == sha256(GOOD), now[:12])
rb = st.one("SELECT kind,decided_by,prev_sha,new_sha FROM skill_releases ORDER BY id DESC LIMIT 1")
check('el rollback queda en el registro', rb[0] == 'rollback' and rb[1] == 'rollback_automatico' and rb[2] == bad_sha and rb[3] == sha256(GOOD))
mon2 = evolve.monitor_production(st, 'memory_rank')
check('después del rollback ya no hay regresión', mon2.get('regressed') is False)

print('\n== 7. Lo ya desplegado antes del registro no se cuenta como automejora ==')
st3 = fresh(); s3 = evolve.register_version(st3, 'memory_rank', GOOD, origin='imported_export')
with st3.tx() as db: db.execute("INSERT INTO skill_pointers VALUES('memory_rank','production',?,?)", (s3, 'x'))
check('se reconstruye una sola fila', evolve.backfill_releases(st3) == 1 and evolve.backfill_releases(st3) == 0)
row = st3.one('SELECT decided_by, generator FROM skill_releases')
check('marcada como reconstruida, con su origen real', row[0] == 'reconstruido' and row[1] == 'origen: imported_export', row)
check('no suma al contador de mejoras', evolve.verified_improvements(st3) == 0)

print(f'\nRESULTADO: {len(OK)} pasan, {len(FAIL)} fallan')
for f in FAIL: print('   FALLA:', f)
sys.exit(1 if FAIL else 0)
