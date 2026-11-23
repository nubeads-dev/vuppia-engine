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


def register_version(store: Store, skill: str, source: str, origin: str, parent_sha=None,
                     generator: str | None = None) -> str:
    candidate_check(source, skill_spec(skill)['func'])
    sha = sha256(source)
    _materialize(skill, sha, source)
    with store.tx() as db:
        db.execute('INSERT OR IGNORE INTO skill_versions(skill,sha,source,parent_sha,origin,created_at,generator) '
                   'VALUES(?,?,?,?,?,?,?)', (skill, sha, source, parent_sha, origin, utc(), generator))
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


# Tolerancia de regresión en el peor régimen (la usan evaluate y monitor_production).
REGRESSION_TOL = 0.005


def evaluate(store: Store, skill: str, candidate_source: str, against: str = 'lab', *,
             record: bool = True, purpose: str = 'candidate') -> dict:
    spec = skill_spec(skill)
    candidate_check(candidate_source, spec['func'])
    # Incumbente a comparar. against='lab' (default): la versión en lab (para promoción a lab).
    # against='production': lo que corre en producción; si producción está vacía, la SEMILLA
    # (el piso real), nunca la propia versión de lab —comparar lab contra lab siempre empata.
    if against == 'seed':
        inc_source = seed_source(skill)                 # piso fijo (para monitoreo de regresión)
        inc_sha = sha256(inc_source)
    elif against == 'production':
        prow = store.one('SELECT v.sha,v.source FROM skill_pointers p JOIN skill_versions v '
                         "ON v.skill=p.skill AND v.sha=p.sha WHERE p.skill=? AND p.channel='production'",
                         (skill,))
        if prow:
            inc_sha, inc_source = prow[0], prow[1]
        else:
            inc_source = seed_source(skill)
            inc_sha = sha256(inc_source)
    else:
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
    # Solo las evaluaciones de CANDIDATOS (y su repetición independiente) van a skill_evals.
    # El monitoreo de lo ya incorporado va a skill_checks (record=False): así re-evaluar el mismo
    # código no se anota como una mejora nueva.
    if record:
        with store.tx() as db:
            cur = db.execute('INSERT INTO skill_evals(skill,candidate_sha,incumbent_sha,at,eval_seed_commit,'
                             'eval_seed,analysis,decision,environment,code_fp,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (skill, cand_sha, inc_sha, utc(), commit, canonical(seeds), canonical(analysis),
                              'improved' if improved else 'rejected', canonical(environment()),
                              canonical(code_fingerprint()), purpose))
            analysis['eval_id'] = cur.lastrowid
    return analysis


def _source_of(store: Store, skill: str, sha: str | None) -> str | None:
    if not sha:
        return None
    row = store.one('SELECT source FROM skill_versions WHERE skill=? AND sha=?', (skill, sha))
    if row:
        return row[0]
    seed = seed_source(skill)
    return seed if sha256(seed) == sha else None


def record_release(store: Store, skill: str, channel: str, kind: str, prev_sha, new_sha: str,
                   decided_by: str, *, analysis: dict | None = None, holdout: dict | None = None,
                   note: str = '') -> int:
    """Registro de versiones: UNA fila por incorporación o rollback, con versión anterior y
    nueva, diff exacto, quién lo decidió, qué modelo escribió el código y las métricas y pruebas
    que lo respaldan. Es la fuente de verdad de 'qué se incorporó y por qué'."""
    import difflib
    old_src = _source_of(store, skill, prev_sha) or seed_source(skill)
    new_src = _source_of(store, skill, new_sha) or ''
    diff = ''.join(difflib.unified_diff(old_src.splitlines(True), new_src.splitlines(True),
                                        (prev_sha or 'semilla')[:12], new_sha[:12]))[:20000]
    gen = store.one('SELECT generator, origin FROM skill_versions WHERE skill=? AND sha=?', (skill, new_sha))
    generator = (gen[0] if gen and gen[0] else ('origen: ' + gen[1] if gen else None))
    metrics = None
    if analysis:
        metrics = {k: analysis.get(k) for k in ('candidate_mean', 'incumbent_mean', 'worst_regime_delta',
                                                 'paired_units', 'replicas_per_regime', 'per_regime',
                                                 'seed_commit', 'incumbent_sha')}
        metrics['paired'] = {k: (analysis.get('paired') or {}).get(k) for k in ('mean_diff', 'ci', 'p_value')}
        if holdout:
            metrics['holdout'] = {k: holdout.get(k) for k in ('candidate_mean', 'incumbent_mean',
                                                             'worst_regime_delta', 'seed_commit')}
    with store.tx() as db:
        cur = db.execute('INSERT INTO skill_releases(skill,channel,kind,prev_sha,new_sha,at,decided_by,generator,'
                         'eval_id,holdout_eval_id,metrics,diff,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (skill, channel, kind, prev_sha, new_sha, utc(), decided_by, generator,
                          (analysis or {}).get('eval_id'), (holdout or {}).get('eval_id'),
                          canonical(metrics) if metrics else None, diff, note[:500]))
        store._append_in_tx('skill_release', {'id': cur.lastrowid, 'skill': skill, 'channel': channel,
                                              'kind': kind, 'prev': prev_sha, 'new': new_sha,
                                              'decided_by': decided_by})
        return cur.lastrowid


def backfill_releases(store: Store) -> int:
    """Las versiones que ya estaban en producción antes del registro no tienen fila. Se
    reconstruye UNA por skill, marcada como reconstruida y con el origen que consta (no se le
    inventa autoría ni métricas: se adjunta la última evaluación registrada, si existe)."""
    n = 0
    for skill, sha in store.q("SELECT skill, sha FROM skill_pointers WHERE channel='production'"):
        if store.one("SELECT 1 FROM skill_releases WHERE skill=? AND channel='production' AND new_sha=?",
                     (skill, sha)):
            continue
        import json
        last = store.one("SELECT id, analysis FROM skill_evals WHERE skill=? AND candidate_sha=? AND "
                         "decision='improved' ORDER BY id DESC LIMIT 1", (skill, sha))
        analysis = None
        if last:
            try:
                analysis = json.loads(last[1]); analysis['eval_id'] = last[0]
            except Exception:                           # noqa: BLE001
                analysis = None
        record_release(store, skill, 'production', 'promotion', sha256(seed_source(skill)), sha,
                       'reconstruido', analysis=analysis,
                       note='Fila reconstruida por la auditoría del 23/11/2026: la incorporación ocurrió '
                            'antes de existir el registro. Sin evaluación independiente ni datos reales.')
        n += 1
    return n


def verified_improvements(store: Store) -> int:
    """Mejoras realmente incorporadas a producción: versiones DISTINTAS con evaluación de
    candidato + repetición independiente. Las reconstruidas o importadas no cuentan."""
    return store.one("SELECT COUNT(DISTINCT skill || ':' || new_sha) FROM skill_releases WHERE "
                     "channel='production' AND kind='promotion' AND holdout_eval_id IS NOT NULL")[0]


def independent_gate(store: Store, skill: str, candidate_source: str, against: str = 'production') -> dict:
    """Puerta de incorporación. Exige TRES cosas:
      1. evaluación del candidato contra la versión vigente (semillas nuevas, comprometidas antes);
      2. una SEGUNDA evaluación independiente, con otras semillas, que también dé mejora;
      3. control A/A: la versión vigente contra sí misma en el mismo arnés debe dar diferencia nula.
    El candidato corre aislado en el sandbox y solo recibe features: no ve semillas, verdad ni
    el banco, y no puede modificarlos."""
    first = evaluate(store, skill, candidate_source, against=against, purpose='candidate')
    out = {'skill': skill, 'first': first, 'passed': False}
    if not first['improved']:
        out['reason'] = 'no mejora en la primera evaluación'
        return out
    second = evaluate(store, skill, candidate_source, against=against, purpose='holdout')
    out['holdout'] = second
    if not second['improved']:
        out['reason'] = 'no se repite la mejora en la evaluación independiente'
        return out
    inc_src = _source_of(store, skill, first['incumbent_sha']) or seed_source(skill)
    aa = evaluate(store, skill, inc_src, against=against, record=False)
    out['aa'] = {'mean_diff': round(aa['candidate_mean'] - aa['incumbent_mean'], 9),
                 'worst_regime_delta': aa['worst_regime_delta'], 'improved': aa['improved']}
    with store.tx() as db:
        db.execute('INSERT INTO skill_checks(skill,sha,baseline_sha,at,kind,analysis,verdict) VALUES(?,?,?,?,?,?,?)',
                   (skill, first['incumbent_sha'], first['incumbent_sha'], utc(), 'aa', canonical(out['aa']),
                    'ok' if (abs(out['aa']['mean_diff']) < 1e-9 and not aa['improved']) else 'falla'))
    if abs(out['aa']['mean_diff']) >= 1e-9 or aa['improved']:
        out['reason'] = 'el control A/A no dio diferencia nula: el arnés no es confiable'
        return out
    out['passed'] = True
    return out


def promote_lab(store: Store, skill: str, candidate_source: str, generator: str | None = None) -> dict:
    analysis = evaluate(store, skill, candidate_source)
    if not analysis['improved']:
        return {'promoted': False, 'reason': 'no supera al incumbente en todos los regímenes OOD',
                'analysis': analysis}
    inc_sha = analysis['incumbent_sha']
    sha = register_version(store, skill, candidate_source, origin='evolved', parent_sha=inc_sha,
                           generator=generator)
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
    record_release(store, skill, 'production', 'promotion', prev, full, 'operador_con_firma',
                   note='aprobación firmada, nonce ' + str(payload['nonce'])[:24])
    return {'promoted_to_production': full, 'prev_sha': prev, 'approval_nonce': payload['nonce']}


# Skills de BAJO RIESGO: funciones de scoring puras, evaluadas en sandbox, que no pueden
# tocar infraestructura, red ni archivos. Son las únicas que el motor puede auto-aplicar a
# producción SIN firma, y solo si VUPPIA_AUTOAPPLY=1 (autorización previa del operador).
LOW_RISK_SKILLS = ('hypothesis', 'memory_rank')


def promote_production_auto(store: Store, skill: str, sha: str, reason: str, gate: dict | None = None) -> dict:
    """Promoción AUTOMÁTICA a producción, SIN firma, SOLO para skills de bajo riesgo y SOLO
    cuando el operador la pre-autorizó (VUPPIA_AUTOAPPLY=1). Registra la versión anterior
    para el rollback. Lo crítico/arquitectura NO pasa por acá: eso siempre exige firma."""
    if skill not in LOW_RISK_SKILLS:
        raise ValueError('auto-aplicación solo permitida para skills de bajo riesgo')
    row = store.one('SELECT sha FROM skill_versions WHERE skill=? AND sha LIKE ?', (skill, sha + '%'))
    if not row:
        raise ValueError('versión inexistente')
    full = row[0]
    prev_row = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    prev = prev_row[0] if prev_row else None
    if prev == full:
        return {'promoted_to_production': full, 'prev_sha': prev, 'noop': True}
    with store.tx() as db:
        if prev_row:
            db.execute("UPDATE skill_pointers SET sha=?,updated_at=? WHERE skill=? AND channel='production'",
                       (full, utc(), skill))
        else:
            db.execute("INSERT INTO skill_pointers VALUES(?,'production',?,?)", (skill, full, utc()))
        store._append_in_tx('promote_production_auto', {'skill': skill, 'sha': full, 'prev_sha': prev,
                                                        'reason': reason})
    record_release(store, skill, 'production', 'promotion', prev, full, 'vuppia_auto',
                   analysis=(gate or {}).get('first'), holdout=(gate or {}).get('holdout'), note=reason)
    return {'promoted_to_production': full, 'prev_sha': prev}


def auto_apply(store: Store, skill: str) -> dict:
    """Cierra el ciclo SOLO: si la versión en lab está adelante de producción y, re-evaluada
    por el juez independiente, MEJORA a la de producción (semilla por defecto) sin regresión,
    la aplica a producción automáticamente. Bajo riesgo + rollback automático la respaldan."""
    if skill not in LOW_RISK_SKILLS:
        return {'skill': skill, 'status': 'no_bajo_riesgo'}
    lab = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='lab'", (skill,))
    prod = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    lab_sha = lab[0] if lab else None
    prod_sha = prod[0] if prod else None
    if not lab_sha or lab_sha == prod_sha:
        return {'skill': skill, 'status': 'al_dia'}
    src = store.one('SELECT source FROM skill_versions WHERE skill=? AND sha=?', (skill, lab_sha))
    if not src:
        return {'skill': skill, 'status': 'fuente_no_encontrada'}
    # No re-evaluar (ni re-registrar) un par candidato/vigente que ya fue rechazado.
    inc_sha = prod_sha or sha256(seed_source(skill))
    if store.one("SELECT 1 FROM skill_evals WHERE skill=? AND candidate_sha=? AND incumbent_sha=? AND "
                 "decision='rejected' AND purpose IN ('candidate','holdout')", (skill, lab_sha, inc_sha)):
        return {'skill': skill, 'status': 'ya_evaluado_sin_mejora'}
    try:
        gate = independent_gate(store, skill, src[0], against='production')
    except Exception as exc:                            # noqa: BLE001
        return {'skill': skill, 'status': 'eval_error', 'error': repr(exc)[:120]}
    if not gate['passed']:
        return {'skill': skill, 'status': 'no_mejora', 'motivo': gate.get('reason'),
                'worst_regime_delta': gate['first'].get('worst_regime_delta')}
    try:
        res = promote_production_auto(store, skill, lab_sha, reason='autoapply:mejora_verificada', gate=gate)
        return {'skill': skill, 'status': 'aplicado', **res}
    except Exception as exc:                            # noqa: BLE001
        return {'skill': skill, 'status': 'error', 'error': repr(exc)[:120]}


def _production_prev(store: Store, skill: str):
    """Versión de producción inmediatamente anterior a la actual, leída de la bitácora."""
    cur = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    cur_sha = cur[0] if cur else None
    # Fuente de verdad: el registro de versiones (cubre firmadas y automáticas).
    rel = store.one("SELECT prev_sha FROM skill_releases WHERE skill=? AND channel='production' AND "
                    "kind='promotion' AND new_sha=? ORDER BY id DESC LIMIT 1", (skill, cur_sha))
    if rel:
        return rel[0]
    rows = store.q("SELECT body FROM ledger WHERE kind IN ('promote_production','promote_production_auto') "
                   "ORDER BY seq DESC LIMIT 50")
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
    _cur = store.one("SELECT sha FROM skill_pointers WHERE skill=? AND channel='production'", (skill,))
    cur_sha = _cur[0] if _cur else None
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
    record_release(store, skill, 'production', 'rollback', cur_sha, target,
                   'rollback_automatico' if reason.startswith('monitor') else 'rollback_operador', note=reason)
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
        analysis = evaluate(store, skill, src_row[0], against='seed', record=False)  # vs SEMILLA (piso)
    except Exception as exc:                             # noqa: BLE001
        return {'skill': skill, 'status': 'eval_error', 'error': repr(exc)[:120]}
    worst = analysis.get('worst_regime_delta')
    regressed = (worst is not None and worst < -REGRESSION_TOL)
    with store.tx() as db:
        db.execute('INSERT INTO skill_checks(skill,sha,baseline_sha,at,kind,analysis,verdict) VALUES(?,?,?,?,?,?,?)',
                   (skill, cur[0], analysis['incumbent_sha'], utc(), 'monitor',
                    canonical({k: analysis.get(k) for k in ('candidate_mean', 'incumbent_mean',
                                                            'worst_regime_delta', 'per_regime', 'seed_commit')}),
                    'regresion' if regressed else 'ok'))
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


# Mejoras de REFERENCIA (curadas): cuando el generador LLM no encuentra una mejora, el
# motor prueba esta versión de referencia. El juez independiente (evaluate) la evalúa igual
# que cualquier candidato: si no mejora de verdad, la rechaza. Para memory_rank: usa las
# señales (trust, specificity, contradiction) que la semilla ignora a propósito.
REFERENCE = {
    'memory_rank': (
        'def rank_memories(item):\n'
        '    overlap = float(item.get("overlap", 0.0))\n'
        '    recency = float(item.get("recency", 0.0))\n'
        '    trust = float(item.get("trust", 0.0))\n'
        '    specificity = float(item.get("specificity", 0.0))\n'
        '    contradiction = float(item.get("contradiction", 0.0))\n'
        '    s = 0.45 * overlap + 0.08 * recency + 0.22 * trust + 0.15 * specificity\n'
        '    s = s * (1.0 - 0.9 * contradiction)\n'
        '    return max(0.0, min(1.0, s))\n'
    ),
}


def try_reference(store: Store, skill: str) -> dict:
    """Prueba la mejora de referencia curada para un skill (respaldo cuando el LLM no mejora).
    El juez independiente decide; si ya está en lab, no reintenta. No lanza hacia afuera."""
    src = REFERENCE.get(skill)
    if not src:
        return {'skill': skill, 'status': 'sin_referencia'}
    try:
        cur_sha, _ = current(store, skill)
        if sha256(src) == cur_sha:
            return {'skill': skill, 'status': 'ya_en_lab'}
        r = promote_lab(store, skill, src)
        return {'skill': skill, 'status': 'promovido' if r.get('promoted') else 'rechazado', **r}
    except Exception as exc:                            # noqa: BLE001
        return {'skill': skill, 'status': 'error', 'error': repr(exc)[:160]}


# Qué modelo escribió el último candidato (lo lee el motor para dejarlo registrado).
LAST_GENERATOR: dict = {'model': None, 'call_id': None}


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
    try:
        raw = coder.complete(system, user, purpose='evolve_generate', max_tokens=1500, temperature=0.6)
    except Exception:                                   # noqa: BLE001
        # Si el modelo fuerte está topado (cuota diaria) u otro error, caemos al del motor.
        if coder is not proposer:
            raw = proposer.complete(system, user, purpose='evolve_generate', max_tokens=1500, temperature=0.6)
            coder = proposer
        else:
            raise
    used = getattr(coder, 'last_used', None) or coder
    LAST_GENERATOR['model'] = f"{getattr(used, 'name', '?')}:{getattr(used, 'model', '?')}"
    LAST_GENERATOR['call_id'] = getattr(used, 'last_call_id', None)
    src = raw.strip()
    if src.startswith('```'):
        src = src.split('\n', 1)[1].rsplit('```', 1)[0]
    candidate_check(src, spec['func'])
    return src.strip() + '\n'
