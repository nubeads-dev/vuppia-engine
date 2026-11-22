"""Autorreprogramación modular v3.1 con aislamiento fuerte, evaluación oculta por
regímenes OOD, reproducibilidad completa y promoción a producción autenticada.

Correcciones sobre 3.0:
#1 Aislamiento total: el candidato corre en SU PROPIO contenedor y recibe SOLO
   vectores de features. La verdad, la semilla y el harness de calificación viven
   en el host; la calificación ocurre fuera del contenedor del candidato. No hay
   fórmula fija conocida por el generador: la verdad es una política oculta del día.
#2 Promoción a producción con firma Ed25519 del operador (ver approval.py).
#3 Se guardan scores crudos por régimen, semillas, entorno y huella de código.
#4 Código no confiable solo en Docker endurecido (ver sandbox.run untrusted=True).
#5 Mejora medida en varios regímenes OOD; debe sostenerse en el PEOR, no en promedio.

La promoción al canal 'lab' es automática si gana; 'production' nunca lo es.
"""
from __future__ import annotations

import ast
import os
import secrets
from pathlib import Path

from . import approval, benchmarks, memory_benchmarks
from .sandbox import image_id, run as sandbox_run
from .stats import paired_analysis
from .store import Store, canonical, code_fingerprint, environment, sha256, utc

HERE = Path(__file__).resolve().parent
RUNNER = HERE / 'candidate_runner.py'
SKILLS_DIR = HERE.parent / 'skills_state'
SEED_DIR = HERE / 'skills_seed'

# Ítems por régimen de evaluación. Mezcla en-distribución y fuera-de-distribución.
ITEMS_PER_REGIME = 400
POLICY_DRAWS = 24                       # promediar políticas ocultas = verdad estable
MARGIN = 0.003                          # mejora mínima relevante por régimen
MEM_QUERIES_PER_REGIME = 120
REPLICAS_PER_REGIME = 3                  # réplicas con semillas distintas por régimen (poder estadístico)

SAFE_NAMES = {'eval', 'exec', 'compile', 'open', 'input', 'globals', 'locals', '__import__',
              'breakpoint', 'getattr', 'setattr', 'delattr', 'vars', 'memoryview'}

# Registro de módulos evolucionables. Cada skill define su función, su semilla y su banco
# de evaluación aislado (genera instancias por régimen y califica fuera del contenedor).
# Pasar de una función a módulos completos = agregar entradas acá, no reescribir el motor.
SKILLS = {
    'hypothesis': {'func': 'score_hypothesis', 'seed': 'hypothesis.py',
                   'regimes': [r[0] for r in benchmarks.REGIMES],
                   'make': lambda reg, seed: benchmarks.make_instance(reg, seed, ITEMS_PER_REGIME, POLICY_DRAWS),
                   'records': lambda inst: benchmarks.items_to_records(inst[0]),
                   'grade': lambda scores, inst: benchmarks.grade(scores, inst[1]),
                   'desc': 'Priorización de hipótesis (triage de experimentos).'},
    'memory_rank': {'func': 'rank_memories', 'seed': 'memory_rank.py',
                    'regimes': [r[0] for r in memory_benchmarks.REGIMES],
                    'make': lambda reg, seed: memory_benchmarks.make_instance(reg, seed, MEM_QUERIES_PER_REGIME),
                    'records': lambda inst: memory_benchmarks.instance_to_records(inst),
                    'grade': lambda scores, inst: memory_benchmarks.grade(scores, inst),
                    'desc': 'Recuperación de memoria: ordena recuerdos por relevancia a una consulta.'},
}


def skill_spec(skill: str) -> dict:
    if skill not in SKILLS:
        raise ValueError('skill evolucionable desconocida: ' + skill)
    return SKILLS[skill]


def seed_source(skill: str) -> str:
    return (SEED_DIR / skill_spec(skill)['seed']).read_text(encoding='utf-8')


def candidate_check(source: str, func_name: str = 'score_hypothesis') -> None:
    if len(source.encode()) > 20000:
        raise ValueError('archivo demasiado grande')
    tree = ast.parse(source)
    forbidden = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
                 ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.Try, ast.Raise, ast.Delete,
                 ast.Yield, ast.YieldFrom, ast.Await)
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            raise ValueError('instrucción no autorizada: ' + type(node).__name__)
        if isinstance(node, ast.Name) and node.id in SAFE_NAMES:
            raise ValueError('función no permitida: ' + node.id)
        if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            raise ValueError('introspección no permitida')
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    allowed = [n for n in tree.body if isinstance(n, ast.FunctionDef)
               or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(funcs) != 1 or funcs[0].name != func_name or len(allowed) != len(tree.body):
        raise ValueError(f'solo se admite la función {func_name} y un docstring')
    if funcs[0].decorator_list:
        raise ValueError('no se permiten decoradores')


def _materialize(skill: str, sha: str, source: str) -> Path:
    SKILLS_DIR.mkdir(exist_ok=True)
    p = SKILLS_DIR / f'{skill}.{sha[:12]}.py'
    if not p.exists():
        p.write_text(source, encoding='utf-8')
    return p


def register_version(store: Store, skill: str, source: str, origin: str, parent_sha=None) -> str:
    candidate_check(source, skill_spec(skill)['func'])
    sha = sha256(source)
    _materialize(skill, sha, source)
    with store.tx() as db:
        db.execute('INSERT OR IGNORE INTO skill_versions(skill,sha,source,parent_sha,origin,created_at) '
                   'VALUES(?,?,?,?,?,?)', (skill, sha, source, parent_sha, origin, utc()))
        if not db.execute('SELECT 1 FROM skill_pointers WHERE skill=? AND channel=?', (skill, 'lab')).fetchone():
            db.execute("INSERT INTO skill_pointers VALUES(?,'lab',?,?)", (skill, sha, utc()))
    return sha


def load_export(store: Store, export_dir: Path) -> dict:
    """Carga las versiones evolucionadas de un export al canal de LAB, verificando el SHA-256
    de cada archivo contra el MANIFEST (corrección 3.6: que una base nueva arranque de la
    versión evolucionada validada, no de la semilla). Producción NO se toca: exige firma Ed25519.
    """
    import json
    manifest_path = Path(export_dir) / 'MANIFEST.json'
    if not manifest_path.exists():
        return {'loaded': [], 'reason': 'no hay MANIFEST.json en ' + str(export_dir)}
    manifest = json.loads(manifest_path.read_text())
    loaded = []
    for entry in manifest.get('skills', []):
        if entry.get('channel') != 'lab' or entry['skill'] not in SKILLS:
            continue
        src_path = Path(export_dir) / entry['file']
        if not src_path.exists():
            continue
        source = src_path.read_text(encoding='utf-8')
        if sha256(source) != entry['sha256']:
            raise ValueError(f'SHA-256 no coincide para {entry["file"]}: export corrupto o alterado')
        candidate_check(source, skill_spec(entry['skill'])['func'])
        register_version(store, entry['skill'], source, origin='imported_export')
        with store.tx() as db:
            db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='lab'",
                       (entry['sha256'], utc(), entry['skill']))
            store._append_in_tx('load_evolved', {'skill': entry['skill'], 'sha': entry['sha256']})
        loaded.append({'skill': entry['skill'], 'sha': entry['sha256'][:12]})
    return {'loaded': loaded, 'export_dir': str(export_dir)}


def current(store: Store, skill: str, channel='lab') -> tuple[str, str]:
    row = store.one('SELECT v.sha,v.source FROM skill_pointers p JOIN skill_versions v '
                    'ON v.skill=p.skill AND v.sha=p.sha WHERE p.skill=? AND p.channel=?', (skill, channel))
    if not row:
        raise ValueError(f'no hay versión en el canal {channel} para {skill}')
    return row[0], row[1]


def _run_candidate(source: str, items: list[dict], func_name: str = 'score_hypothesis') -> list[float]:
    """Ejecuta el candidato AISLADO: solo recibe features, devuelve scores. Verdad fuera."""
    files = {'candidate.py': source.encode(), 'candidate_runner.py': RUNNER.read_bytes()}
    res = sandbox_run(files, 'candidate_runner.py', {'items': items, 'func': func_name},
                      untrusted=True, timeout=60)
    out = res['result']
    if out.get('error') or out.get('scores') is None:
        raise ValueError('candidato inválido: ' + str(out.get('error')))
    return out['scores']


def _eval_on_regime(source: str, regime: str, seed: int, spec: dict) -> dict:
    inst = spec['make'](regime, seed)
    records = spec['records'](inst)
    try:
        scores = _run_candidate(source, records, spec['func'])
    except ValueError as e:
        return {'score': 0.0, 'error': str(e)}
    return spec['grade'](scores, inst)


def evaluate(store: Store, skill: str, candidate_source: str) -> dict:
    spec = skill_spec(skill)
    candidate_check(candidate_source, spec['func'])
    inc_sha, inc_source = current(store, skill)
    cand_sha = sha256(candidate_source)
    # Comprometer las semillas (una por régimen) ANTES de evaluar.
    seeds = {r: secrets.randbelow(2**31) for r in spec['regimes']}
    commit = sha256(canonical({'skill': skill, 'candidate': cand_sha, 'incumbent': inc_sha,
                               'regimes': spec['regimes'], 'seeds': seeds, 'image': image_id()}))
    store.append('eval_seed_commit', {'skill': skill, 'candidate_sha': cand_sha, 'commit': commit})
    # Réplicas por régimen: más unidades pareadas = más poder estadístico. Cada (régimen,réplica)
    # usa una semilla derivada determinista del commit, para reproducibilidad.
    replicas = REPLICAS_PER_REGIME
    per_regime = []
    paired_cand, paired_inc = [], []     # unidades pareadas (régimen × réplica) para la significancia
    cand_scores, inc_scores = [], []     # medias por régimen (para no-regresión)
    candidate_errors = []
    for regime, seed in seeds.items():
        reg_c, reg_i = [], []
        for k in range(replicas):
            rseed = int(sha256(f'{commit}:{regime}:{k}')[:12], 16)
            c = _eval_on_regime(candidate_source, regime, rseed, spec)
            i = _eval_on_regime(inc_source, regime, rseed, spec)
            if 'error' in c:
                candidate_errors.append({'regime': regime, 'error': c['error']})
            reg_c.append(c['score']); reg_i.append(i['score'])
            paired_cand.append(c['score']); paired_inc.append(i['score'])
        cmean = sum(reg_c) / len(reg_c); imean = sum(reg_i) / len(reg_i)
        per_regime.append({'regime': regime, 'candidate': {'score': round(cmean, 6)},
                           'incumbent': {'score': round(imean, 6)},
                           'delta': round(cmean - imean, 6), 'replicas': replicas})
        cand_scores.append(cmean); inc_scores.append(imean)
    no_error = len(candidate_errors) == 0
    # Análisis pareado sobre todas las unidades (régimen × réplica).
    stat = paired_analysis(paired_cand, paired_inc, min_effect=MARGIN, alpha=0.05,
                           higher_is_better=True, seed=1, check_triviality=False)
    worst_delta = min(r['delta'] for r in per_regime)
    # Criterio: (a) NO regresa en ningún régimen OOD (tolerancia de ruido), y
    # (b) mejora AGREGADA significativa por encima del margen. Así un candidato que gana
    # en 5 regímenes pero rompe 1 (p. ej. sobreajuste a la saturación) NO se promueve.
    REGRESSION_TOL = 0.005
    no_regression = worst_delta > -REGRESSION_TOL
    aggregate_improves = stat['ci'][0] > MARGIN and stat['p_value'] < 0.05
    improved = bool(no_error and no_regression and aggregate_improves and worst_delta > 0)
    analysis = {'skill': skill, 'candidate_sha': cand_sha, 'incumbent_sha': inc_sha,
                'seed_commit': commit, 'seeds': seeds, 'per_regime': per_regime,
                'candidate_mean': round(sum(cand_scores) / len(cand_scores), 6),
                'incumbent_mean': round(sum(inc_scores) / len(inc_scores), 6),
                'worst_regime_delta': worst_delta, 'paired': stat,
                'improved': improved, 'no_regression': no_regression, 'aggregate_improves': aggregate_improves,
                'replicas_per_regime': replicas, 'paired_units': len(paired_cand),
                'candidate_errors': candidate_errors}
    with store.tx() as db:
        db.execute('INSERT INTO skill_evals(skill,candidate_sha,incumbent_sha,at,eval_seed_commit,eval_seed,'
                   'analysis,decision,environment,code_fp) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (skill, cand_sha, inc_sha, utc(), commit, canonical(seeds), canonical(analysis),
                    'improved' if improved else 'rejected', canonical(environment()),
                    canonical(code_fingerprint())))
    return analysis


def promote_lab(store: Store, skill: str, candidate_source: str) -> dict:
    analysis = evaluate(store, skill, candidate_source)
    if not analysis['improved']:
        return {'promoted': False, 'reason': 'no supera al incumbente en todos los regímenes OOD',
                'analysis': analysis}
    inc_sha = analysis['incumbent_sha']
    sha = register_version(store, skill, candidate_source, origin='evolved', parent_sha=inc_sha)
    with store.tx() as db:
        db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='lab'",
                   (sha, utc(), skill))
        store._append_in_tx('promote_lab', {'skill': skill, 'from': inc_sha, 'to': sha,
                                            'worst_regime_delta': analysis['worst_regime_delta']})
    return {'promoted': True, 'channel': 'lab', 'skill': skill, 'from_sha': inc_sha, 'to_sha': sha,
            'analysis': analysis}


def rollback_lab(store: Store, skill: str, to_sha: str) -> dict:
    row = store.one('SELECT sha FROM skill_versions WHERE skill=? AND sha LIKE ?', (skill, to_sha + '%'))
    if not row:
        raise ValueError('versión no registrada para rollback')
    with store.tx() as db:
        db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='lab'",
                   (row[0], utc(), skill))
        store._append_in_tx('rollback_lab', {'skill': skill, 'to': row[0]})
    return {'rolled_back_to': row[0], 'channel': 'lab'}


def promote_production(store: Store, skill: str, sha: str, approval_token: str) -> dict:
    """Promoción a producción: exige firma Ed25519 válida del operador (corrección #2).
    El token es de un solo uso (nonce en bitácora) y con vencimiento."""
    row = store.one('SELECT sha,source FROM skill_versions WHERE skill=? AND sha LIKE ?', (skill, sha + '%'))
    if not row:
        raise ValueError('versión inexistente')
    full = row[0]
    payload = approval.verify(approval_token, skill, full)   # lanza si firma/ventana/destino fallan
    tid = approval.token_id(approval_token)
    prev_row = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    prev = prev_row[0] if prev_row else None             # versión anterior, para rollback
    with store.tx() as db:
        if db.execute('SELECT 1 FROM approvals_used WHERE token_id=?', (tid,)).fetchone():
            raise ValueError('esta aprobación ya fue usada (anti-replay)')
        db.execute('INSERT INTO approvals_used(token_id,skill,candidate_sha,used_at,nonce,expires) '
                   'VALUES(?,?,?,?,?,?)', (tid, skill, full, utc(), payload['nonce'], payload['expires']))
        if prev_row:
            db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='production'",
                       (full, utc(), skill))
        else:
            db.execute("INSERT INTO skill_pointers VALUES(?,'production',?,?)", (skill, full, utc()))
        store._append_in_tx('promote_production', {'skill': skill, 'sha': full, 'prev_sha': prev,
                                                   'approval_nonce': payload['nonce']})
    return {'promoted_to_production': full, 'prev_sha': prev, 'approval_nonce': payload['nonce']}


def _production_prev(store: Store, skill: str):
    """Versión de producción inmediatamente anterior a la actual, leída de la bitácora."""
    rows = store.q("SELECT body FROM ledger WHERE kind='promote_production' ORDER BY seq DESC LIMIT 50")
    cur = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    cur_sha = cur[0] if cur else None
    for (body,) in rows:
        try:
            ev = json.loads(body)
        except Exception:                               # noqa: BLE001
            continue
        if ev.get('skill') == skill and ev.get('sha') == cur_sha:
            return ev.get('prev_sha')
    return None


def rollback_production(store: Store, skill: str, reason: str = 'manual') -> dict:
    """Revierte producción a la versión anterior (o a la semilla si no hay anterior).
    Es una acción de SEGURIDAD: vuelve a una versión ya probada, por eso NO exige firma."""
    prev = _production_prev(store, skill)
    target = prev
    label = 'version anterior'
    if not target:
        # Sin anterior registrada: volver a la semilla (siempre segura).
        target = register_version(store, skill, seed_source(skill), origin='seed_rollback')
        label = 'semilla'
    row = store.one('SELECT sha FROM skill_versions WHERE skill=? AND sha=?', (skill, target))
    if not row:
        raise ValueError('versión destino de rollback no registrada')
    with store.tx() as db:
        if db.execute("SELECT 1 FROM skill_pointers WHERE skill=? AND channel='production'", (skill,)).fetchone():
            db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='production'",
                       (target, utc(), skill))
        else:
            db.execute("INSERT INTO skill_pointers VALUES(?,'production',?,?)", (skill, target, utc()))
        store._append_in_tx('rollback_production', {'skill': skill, 'to': target, 'reason': reason})
    return {'rolled_back_to': target, 'label': label, 'reason': reason}


def monitor_production(store: Store, skill: str) -> dict:
    """Monitoreo + rollback AUTOMÁTICO: re-evalúa la versión en producción contra la semilla.
    Si regresó (empeora el peor régimen más allá de la tolerancia), revierte sola y lo registra.
    Devuelve el diagnóstico. No lanza hacia afuera."""
    cur = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    if not cur or not cur[0]:
        return {'skill': skill, 'status': 'sin_produccion'}
    src_row = store.one('SELECT source FROM skill_versions WHERE skill=? AND sha=?', (skill, cur[0]))
    if not src_row:
        return {'skill': skill, 'status': 'fuente_no_encontrada'}
    try:
        analysis = evaluate(store, skill, src_row[0])   # re-evalúa vs incumbente (semilla por defecto)
    except Exception as exc:                             # noqa: BLE001
        return {'skill': skill, 'status': 'eval_error', 'error': repr(exc)[:120]}
    worst = analysis.get('worst_regime_delta')
    regressed = (worst is not None and worst < -REGRESSION_TOL)
    out = {'skill': skill, 'prod_sha': cur[0][:12], 'worst_regime_delta': worst, 'regressed': regressed}
    if regressed:
        try:
            rb = rollback_production(store, skill, reason=f'monitor:regresion({worst:.4f})')
            out['rolled_back_to'] = rb['rolled_back_to'][:12]
            out['rollback_label'] = rb['label']
        except Exception as exc:                        # noqa: BLE001
            out['rollback_error'] = repr(exc)[:120]
    return out


PROMPTS = {
    'score_hypothesis': ('Mejorás UNA función Python aislada de priorización, no la conciencia. '
        'Devolvés SOLO código Python válido que defina score_hypothesis(item). Sin markdown, sin '
        'imports, sin exec/eval, sin red ni archivos, sin clases ni try. item es un dict con evidence, '
        'falsifiability, feasibility, importance (típicamente en [0,1]). La salida debe quedar en [0,100] '
        'y ser monótona creciente en cada variable. El objetivo es GENERALIZAR, no memorizar casos.'),
    'rank_memories': ('Mejorás UNA función Python aislada de recuperación de memoria, no la conciencia. '
        'Devolvés SOLO código Python válido que defina rank_memories(item). Sin markdown, sin imports, '
        'sin exec/eval, sin red ni archivos, sin clases ni try. item es un dict con overlap, recency, '
        'trust, specificity, contradiction (en [0,1]; contradiction es 1 si el recuerdo contradice la '
        'consulta). La salida es una relevancia en [0,1]: debe subir con overlap, trust y specificity, '
        'y BAJAR fuerte cuando contradiction=1. Generalizar, no memorizar casos.'),
}


def generate_candidate(store: Store, skill: str, proposer) -> str:
    import json
    spec = skill_spec(skill)
    _, inc_source = current(store, skill)
    system = PROMPTS[spec['func']]
    # Feedback del último intento: que el cerebro aprenda del rechazo en vez de tirar a ciegas.
    hint = ''
    last = store.one('SELECT analysis,decision FROM skill_evals WHERE skill=? ORDER BY id DESC LIMIT 1', (skill,))
    if last and last[1] != 'improved':
        try:
            a = json.loads(last[0])
            errs = a.get('candidate_errors') or []
            if errs:
                hint = ('\n\nTu intento ANTERIOR fue RECHAZADO porque el código fallaba al ejecutarse: '
                        + str(errs[:2])[:300] + '. Escribí código que corra sin errores.')
            elif not a.get('no_regression', True):
                hint = ('\n\nTu intento ANTERIOR fue RECHAZADO porque empeoraba en algún régimen (peor delta '
                        + str(a.get('worst_regime_delta')) + '). Mejorá SIN romper ningún régimen.')
            else:
                hint = ('\n\nTu intento ANTERIOR no superó al actual de forma significativa. Probá un cambio '
                        'más audaz (no cosmético), manteniendo la robustez en todos los regímenes.')
        except Exception:
            pass
    user = ('Módulo actual:\n' + inc_source + hint + '\n\nProponé una versión mejor. Fijate si el módulo '
            'actual IGNORA señales disponibles (en item) que deberían importar: usalas. Solo el código.')
    # Generar código es ocasional pero la calidad importa mucho: usamos el modelo FUERTE
    # (120b) para proponer, no el 20b del motor. El worker tiene GROQ_API_KEY, así que lo
    # construimos directo si no hay un coder configurado por env.
    coder = None
    try:
        from .providers import from_env, Groq
        coder = from_env(store, 'coder') or from_env(store, 'chat')
        if coder is None and os.getenv('GROQ_API_KEY'):
            coder = Groq(store, 'openai/gpt-oss-120b')
    except Exception:                                   # noqa: BLE001
        coder = None
    coder = coder or proposer
    raw = coder.complete(system, user, purpose='evolve_generate', max_tokens=4000, temperature=0.6)
    src = raw.strip()
    if src.startswith('```'):
        src = src.split('\n', 1)[1].rsplit('```', 1)[0]
    candidate_check(src, spec['func'])
    return src.strip() + '\n'
