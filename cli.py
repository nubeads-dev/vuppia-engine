#!/usr/bin/env python3
"""CLI del worker de VUPPIA SOFÍA 3. Ejecutar desde worker/:  python3 cli.py <cmd>"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vuppia import evolve, memory, mission            # noqa: E402
from vuppia.evolve import current as skill_current     # noqa: E402
from vuppia.protocols import PROTOCOLS                  # noqa: E402
from vuppia.providers import from_env                   # noqa: E402
from vuppia.sandbox import docker_available, image_id   # noqa: E402
from vuppia.store import Store                           # noqa: E402

DB = Path(os.getenv('VUPPIA_DB', Path(__file__).resolve().parent / 'data' / 'vuppia.sqlite3'))


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


EXPORT_DIR = Path(__file__).resolve().parent / 'evolved_export'


def ensure_seed_skill(store):
    fresh = False
    for skill in evolve.SKILLS:
        try:
            skill_current(store, skill)
        except ValueError:
            evolve.register_version(store, skill, evolve.seed_source(skill), origin='seed')
            fresh = True
    # En una base nueva, si hay versiones evolucionadas validadas en el export, cargarlas al LAB
    # (verificando SHA). Así SOFÍA NO arranca de la semilla cuando ya hay una versión mejor.
    # Se puede desactivar con VUPPIA_NO_AUTOLOAD_EVOLVED=1.
    if fresh and EXPORT_DIR.exists() and os.getenv('VUPPIA_NO_AUTOLOAD_EVOLVED') != '1':
        try:
            evolve.load_export(store, EXPORT_DIR)
        except ValueError:
            pass   # export corrupto → se queda con la semilla, nunca rompe el arranque


def main(argv=None):
    p = argparse.ArgumentParser(description='VUPPIA SOFÍA 3 — misión y evolución (no prueba de conciencia)')
    p.add_argument('--db', type=Path, default=DB)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    sub.add_parser('verify-ledger')
    sp = sub.add_parser('plan'); sp.add_argument('--ai', action='store_true'); sp.add_argument('--policy', action='store_true')
    sp.add_argument('--research', action='store_true'); sp.add_argument('--online', action='store_true')
    sp.add_argument('--research-question', default='Scientific approaches to testing AI metacognition and self-improvement')
    sub.add_parser('prereg').add_argument('--id', type=int, required=True)
    r = sub.add_parser('run'); r.add_argument('--id', type=int, required=True)
    c = sub.add_parser('cycle'); c.add_argument('--rounds', type=int, default=3)
    c.add_argument('--ai', action='store_true'); c.add_argument('--policy', action='store_true')
    c.add_argument('--research', action='store_true'); c.add_argument('--online', action='store_true')
    c.add_argument('--research-question', default='Scientific approaches to testing AI metacognition and self-improvement')
    ab = sub.add_parser('ab-test'); ab.add_argument('--trials', type=int, default=60)
    mab = sub.add_parser('mem-ab'); mab.add_argument('--trials', type=int, default=40)
    msh = sub.add_parser('mem-shadow', help='A/B de recuperación REAL (FTS vs FTS+rerank) en observación')
    msh.add_argument('--seed-fixtures', action='store_true', help='Sembrar recuerdos reales etiquetados')
    msh.add_argument('--promote', action='store_true', help='Pasar rerank a active si la mejora es consistente')
    ex = sub.add_parser('export-skills', help='Exporta versiones evolucionadas activas con hash y evidencia')
    ex.add_argument('--out', type=Path, default=Path('evolved_export'))
    aut = sub.add_parser('autonomous', help='Runner autónomo 24/7 (loop supervisado con guardas)')
    aut.add_argument('--interval', type=int, default=900, help='segundos entre ticks')
    aut.add_argument('--max-iterations', type=int, default=0, help='0 = infinito hasta freno')
    aut.add_argument('--once', action='store_true', help='un solo tick y salir')
    aut.add_argument('--stop-file', default='VUPPIA_STOP')
    sub.add_parser('heartbeat', help='Muestra el último latido del runner autónomo')
    ak = sub.add_parser('ask', help='Hablar con SOFÍA (plano de control sobre todo el sistema)')
    ak.add_argument('question'); ak.add_argument('--with-ai', action='store_true')
    ak.add_argument('--online', action='store_true')
    ag = sub.add_parser('agenda', help='Ver/editar la agenda de investigaciones')
    ag.add_argument('--status'); ag.add_argument('--add'); ag.add_argument('--suspend', type=int)
    ag.add_argument('--prioritize', type=int)
    sub.add_parser('objectives', help='Objetivos corto/mediano/largo plazo y progreso real')
    sub.add_parser('history', help='Historial de conversación con SOFÍA')
    au = sub.add_parser('audit-llm'); au.add_argument('--id', type=int)
    sub.add_parser('report')
    m = sub.add_parser('memory'); m.add_argument('--search', default=''); m.add_argument('--kind')
    ev = sub.add_parser('evolve')
    ev.add_argument('mode', choices=['status', 'generate-eval', 'evaluate-file', 'promote-lab',
                                     'rollback', 'promote-production'])
    ev.add_argument('--skill', default='hypothesis'); ev.add_argument('--file', type=Path)
    ev.add_argument('--sha'); ev.add_argument('--approval-token', default='')
    rp = sub.add_parser('reproduce'); rp.add_argument('--id', type=int, required=True)
    sub.add_parser('keygen')
    rs = sub.add_parser('research', help='Investigar con fuentes web y, opcionalmente, varias IA')
    rs.add_argument('--question', required=True)
    rs.add_argument('--online', action='store_true', help='Habilita consulta a APIs externas')
    rs.add_argument('--with-ai', action='store_true', help='Usa investigador y auditor configurados')
    rs.add_argument('--sources', default='openalex,crossref', help='openalex,crossref,brave')
    rs.add_argument('--max-steps', type=int, default=2, choices=[1, 2, 3])
    rs.add_argument('--per-source', type=int, default=4)
    args = p.parse_args(argv)

    store = Store(args.db)
    ensure_seed_skill(store)
    try:
        if args.cmd == 'status':
            out({'version': '3.8.0', 'first_law': mission.__doc__.splitlines()[0],
                 'docker': docker_available(), 'sandbox_image': image_id(),
                 'protocols': {k: v['kind'] for k, v in PROTOCOLS.items()},
                 'providers_configured': {r: bool(from_env(store, r)) for r in ('proposer', 'reviewer', 'subject', 'researcher', 'verifier')},
                 'daily_budget_usd': float(os.getenv('VUPPIA_DAILY_BUDGET_USD', '1.00')),
                 'spent_today_usd': round(store.one(
                     "SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) FROM llm_calls WHERE day=date('now')"
                 )[0], 4), 'hypotheses': store.one('SELECT COUNT(*) FROM hypotheses')[0],
                 'ledger': store.verify_ledger(),
                 'note': 'Ningún componente mide ni afirma experiencia subjetiva.'})
        elif args.cmd == 'verify-ledger':
            out(store.verify_ledger())
        elif args.cmd == 'plan':
            if args.research:
                _maybe_research(store, args)
            if args.ai:
                prop = from_env(store, 'proposer')
                if not prop:
                    sys.exit('Configurá VUPPIA_PROPOSER=anthropic:modelo (y ANTHROPIC_API_KEY)')
                out(mission.plan_ai(store, prop))
            elif args.policy:
                out(mission.plan_policy(store))
            else:
                out(mission.plan_rule(store))
        elif args.cmd == 'prereg':
            out(mission.preregister(store, args.id))
        elif args.cmd == 'run':
            out(mission.execute(store, args.id, subject=from_env(store, 'subject')))
        elif args.cmd == 'cycle':
            results = []
            if args.research:
                _maybe_research(store, args)
            prop = from_env(store, 'proposer') if args.ai else None
            for _ in range(args.rounds):
                if args.ai:
                    plan = mission.plan_ai(store, prop)
                elif args.policy:
                    plan = mission.plan_policy(store)
                else:
                    plan = mission.plan_rule(store)
                mission.preregister(store, plan['id'])
                results.append(mission.execute(store, plan['id'], subject=from_env(store, 'subject')))
            out({'rounds': results})
        elif args.cmd == 'ab-test':
            from vuppia import evolve, planner
            inc_sha, inc_src = evolve.current(store, 'hypothesis', channel='production') \
                if store.one("SELECT 1 FROM skill_pointers WHERE channel='production'") \
                else evolve.current(store, 'hypothesis', channel='lab')
            seed_src = evolve.seed_source('hypothesis')
            out(planner.ab_test(store, seed_src, inc_src, trials=args.trials))
        elif args.cmd == 'mem-ab':
            from vuppia import evolve, memory_eval
            lab_src = evolve.current(store, 'memory_rank', 'lab')[1]
            out(memory_eval.retrieval_ab(store, evolve.seed_source('memory_rank'), lab_src, trials=args.trials))
        elif args.cmd == 'mem-shadow':
            from vuppia import memory_fixtures, memory_shadow
            if args.seed_fixtures:
                labeled = memory_fixtures.build(store)
            else:
                sys.exit('Esta demo necesita --seed-fixtures (recuerdos reales etiquetados). '
                         'En producción, pasá un set etiquetado de la actividad real de SOFÍA.')
            result = {'test': memory_shadow.evaluate(store, labeled, split='test'),
                      'dev': memory_shadow.evaluate(store, labeled, split='dev')}
            test = result['test']
            # mem-shadow NO activa nada: solo informa elegibilidad sobre el conjunto TEST reservado.
            # La activación real = promover memory_rank a PRODUCCIÓN con firma Ed25519 (ver abajo),
            # y recién entonces search(rerank_mode='active') usa esa versión.
            eligible = test.get('consistent_improvement') and test.get('no_regression')
            result['activation'] = {
                'eligible_on_test': bool(eligible),
                'como_activar': ('Promover memory_rank a producción con firma: cli.py evolve '
                                 'promote-production --skill memory_rank --sha <sha> --approval-token <token>. '
                                 'Solo entonces search(rerank_mode="active") usa el ranker.'),
                'advertencia': ('Estos casos son construidos para la prueba, NO datos operativos. '
                                'Antes de activar en producción hace falta 100–200 consultas reales '
                                'etiquetadas independientemente, con split dev/test.')}
            out(result)
        elif args.cmd == 'export-skills':
            out(_export_skills(store, args.out))
        elif args.cmd == 'autonomous':
            from vuppia import autonomous
            if args.once:
                autonomous._ensure_kv(store)
                out(autonomous.tick(store, {'stop_file': args.stop_file, 'experiments': True,
                                            'evolve': True, 'evolve_skills': ['hypothesis', 'memory_rank']}))
            else:
                store.db.close()
                out(autonomous.run_forever(args.db, interval_s=args.interval,
                                           max_iterations=args.max_iterations, stop_file=args.stop_file))
                return
        elif args.cmd == 'ask':
            from vuppia import converse
            prov = from_env(store, 'researcher') or from_env(store, 'proposer') if args.with_ai else None
            verif = from_env(store, 'verifier') if args.with_ai else None
            res = converse.ask(store, args.question, provider=prov, verifier=verif, online=args.online)
            print(res['answer'])
            if res.get('proposed') or res.get('needs'):
                print('\n[estado] propuesto:', res.get('proposed'), '| necesita:', res.get('needs'))
        elif args.cmd == 'agenda':
            from vuppia import agenda as ag
            if args.add:
                aid = ag.add(store, args.add, args.add, status='proposed', origin='user')
                out({'agregado_como_propuesta': aid})
            elif args.suspend:
                out({'suspendida': ag.set_status(store, args.suspend, 'suspended')})
            elif args.prioritize:
                ag.set_status(store, args.prioritize, 'active')
                out({'priorizada': ag.set_priority(store, args.prioritize, 10.0)})
            else:
                out(ag.listing(store, args.status))
        elif args.cmd == 'objectives':
            from vuppia import objectives
            out({'objetivos': objectives.objectives(store), 'progreso': objectives.progress(store)})
        elif args.cmd == 'history':
            from vuppia import converse
            out(converse.history(store))
        elif args.cmd == 'heartbeat':
            autonomous_ensure(store)
            row = store.one("SELECT v FROM kv WHERE k='autonomous_heartbeat'")
            out({'heartbeat': row[0] if row else 'sin latidos todavía',
                 'recent_ticks': store.q("SELECT at,body FROM ledger WHERE kind='autonomous_tick' "
                                         'ORDER BY seq DESC LIMIT 5')})
        elif args.cmd == 'audit-llm':
            rows = store.q('SELECT id,at,provider,model,purpose,temperature,max_tokens,input_tokens,'
                           'output_tokens,cost_usd,status,hypothesis_id,substr(response_text,1,200) '
                           'FROM llm_calls' + (' WHERE hypothesis_id=?' if args.id else '') + ' ORDER BY id',
                           (args.id,) if args.id else ())
            cols = ['id', 'at', 'provider', 'model', 'purpose', 'temperature', 'max_tokens', 'input_tokens',
                    'output_tokens', 'cost_usd', 'status', 'hypothesis_id', 'response_preview']
            out({'llm_calls': [dict(zip(cols, r)) for r in rows],
                 'total_cost_usd': round(store.one('SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls')[0], 6)})
        elif args.cmd == 'report':
            rows = store.q('SELECT id,origin,protocol,status,forecast FROM hypotheses ORDER BY id DESC LIMIT 50')
            out({'findings': memory.recent(store, 20, kind='finding'),
                 'hypotheses': [dict(zip(['id', 'origin', 'protocol', 'status', 'forecast'], r)) for r in rows]})
        elif args.cmd == 'memory':
            out(memory.search(store, args.search) if args.search else memory.recent(store, 30, kind=args.kind))
        elif args.cmd == 'evolve':
            _evolve(store, args)
        elif args.cmd == 'research':
            from vuppia import research
            source_names = tuple(x.strip() for x in args.sources.split(',') if x.strip())
            researcher = from_env(store, 'researcher') if args.with_ai else None
            verifier = from_env(store, 'verifier') if args.with_ai else None
            if args.with_ai and not researcher:
                sys.exit('Falta VUPPIA_RESEARCHER (modelo y clave como secretos del host)')
            if args.with_ai and not verifier:
                sys.exit('Falta VUPPIA_VERIFIER (modelo independiente y clave del host)')
            if args.with_ai and os.getenv('VUPPIA_RESEARCHER') == os.getenv('VUPPIA_VERIFIER'):
                sys.exit('Elegí modelos diferentes para VUPPIA_RESEARCHER y VUPPIA_VERIFIER')
            out(research.investigate(store, args.question, sources=source_names,
                                     online=args.online, researcher=researcher, verifier=verifier,
                                     max_steps=args.max_steps, per_source=args.per_source))
        elif args.cmd == 'keygen':
            from vuppia import approval
            priv, pub = approval.keygen()
            out({'guardar_offline_operador (VUPPIA_APPROVAL_PRIVKEY)': priv,
                 'cargar_en_worker (VUPPIA_APPROVAL_PUBKEY)': pub,
                 'nota': 'La privada NUNCA va al worker ni al chat. Firmá con: python3 -m vuppia.approval sign'})
        elif args.cmd == 'reproduce':
            out(_reproduce(store, args.id))
    finally:
        store.db.close()


def _maybe_research(store, args):
    if not args.online:
        sys.exit('--research necesita --online para consultar fuentes externas')
    from vuppia import research
    sources = ('openalex', 'crossref', 'brave') if os.getenv('BRAVE_SEARCH_API_KEY') else ('openalex', 'crossref')
    result = research.investigate(store, args.research_question, online=True,
                                  sources=sources, max_steps=1)
    # Informe breve a stderr; stdout preserva el JSON del plan/resultado.
    print(f"Investigación externa: {len(result['evidence'])} referencias, "
          f"{len(result['errors'])} errores, registro {result['research_id']}", file=sys.stderr)


def autonomous_ensure(store):
    with store.tx() as db:
        db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')


def _export_skills(store, out_dir):
    """Empaqueta las versiones evolucionadas activas con su hash y su evidencia de evaluación,
    para reproducibilidad (corrección 3.5: el ranker evolucionado debe venir en el paquete)."""
    from vuppia import evolve
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'exported_at': __import__('vuppia.store', fromlist=['utc']).utc(), 'skills': []}
    for skill in evolve.SKILLS:
        for channel in ('lab', 'production'):
            row = store.one('SELECT v.sha,v.source,v.origin FROM skill_pointers p JOIN skill_versions v '
                            'ON v.skill=p.skill AND v.sha=p.sha WHERE p.skill=? AND p.channel=?', (skill, channel))
            if not row:
                continue
            sha, src, origin = row
            fname = f'{skill}.{channel}.{sha[:12]}.py'
            (out_dir / fname).write_text(src, encoding='utf-8')
            ev = store.one('SELECT analysis FROM skill_evals WHERE skill=? AND candidate_sha=? '
                           'ORDER BY id DESC LIMIT 1', (skill, sha))
            entry = {'skill': skill, 'channel': channel, 'sha256': sha, 'origin': origin, 'file': fname,
                     'has_eval_evidence': ev is not None}
            if ev:
                (out_dir / f'{skill}.{channel}.{sha[:12]}.eval.json').write_text(ev[0], encoding='utf-8')
                entry['eval_file'] = f'{skill}.{channel}.{sha[:12]}.eval.json'
            manifest['skills'].append(entry)
    (out_dir / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return {'exported_to': str(out_dir), 'skills': manifest['skills'],
            'note': 'Cada versión activa viene con su código, su hash y la evidencia de evaluación.'}


def _reproduce(store, hid):
    """Re-ejecuta un experimento desde su preregistro sellado y compara con lo guardado."""
    from vuppia import mission
    from vuppia.protocols import PROTOCOLS
    PROTO_KIND = {k: v['kind'] for k, v in PROTOCOLS.items()}
    pre = store.one('SELECT record FROM preregistrations WHERE hypothesis_id=?', (hid,))
    orig = store.one('SELECT r.verdict,r.analysis FROM results r JOIN preregistrations p ON r.prereg_id=p.id '
                     'WHERE p.hypothesis_id=?', (hid,))
    if not pre or not orig:
        return {'error': 'no hay preregistro o resultado para ese id'}
    import json
    record = json.loads(pre[0])
    obs = mission._collect_observations(store, record, None)
    from vuppia.stats import paired_analysis
    rerun = paired_analysis(obs['test'], obs['control'], min_effect=record['min_effect'],
                            alpha=record['alpha'], higher_is_better=record['higher_is_better'],
                            seed=record['n_units'])
    orig_a = json.loads(orig[1])

    def close(a, b, tol=1e-6):
        return a is not None and b is not None and abs(a - b) <= tol

    metrics = {}
    for k in ('mean_diff', 'test_mean', 'control_mean', 'p_value', 'effect_size_dz'):
        metrics[k] = {'original': orig_a.get(k), 'rerun': rerun.get(k),
                      'match': close(orig_a.get(k), rerun.get(k))}
    ci_match = (orig_a.get('ci') and rerun.get('ci')
                and close(orig_a['ci'][0], rerun['ci'][0]) and close(orig_a['ci'][1], rerun['ci'][1]))
    # ¿El protocolo usó un LLM? Entonces la salida no es determinista: reproducción APROXIMADA.
    is_llm = PROTO_KIND.get(record['protocol']) == 'llm'
    full_match = orig[0] == rerun['verdict'] and all(m['match'] for m in metrics.values()) and ci_match
    result = {'hypothesis_id': hid, 'protocol': record['protocol'], 'deterministic': not is_llm,
              'verdict': {'original': orig[0], 'rerun': rerun['verdict'], 'match': orig[0] == rerun['verdict']},
              'metrics': metrics, 'ci_match': bool(ci_match),
              'reproducible_exact': bool(full_match)}
    if is_llm:
        calls = store.q('SELECT model,temperature,max_tokens,cost_usd FROM llm_calls WHERE hypothesis_id=?', (hid,))
        result['llm_audit'] = {'calls': len(calls),
                               'models': sorted({c[0] for c in calls}),
                               'total_cost_usd': round(sum(c[3] or 0 for c in calls), 6),
                               'nota': 'Salida de LLM no determinista: se comparan versiones de modelo, '
                                       'parámetros y costos registrados; la reproducción es aproximada '
                                       '(intervalos que se solapan), no exacta.'}
        result['reproducible_approx'] = orig[0] == rerun['verdict']
    result['note'] = ('Protocolo determinista: mismo sello y semillas → mismas métricas.' if not is_llm
                      else 'Protocolo con LLM: reproducción aproximada, ver llm_audit.')
    return result


def _evolve(store, args):
    skill = args.skill
    if args.mode == 'status':
        out({'skill': skill, 'lab_sha': skill_current(store, skill)[0],
             'versions': store.q('SELECT sha,origin,created_at FROM skill_versions WHERE skill=? ORDER BY id', (skill,)),
             'recent_evals': store.q('SELECT candidate_sha,decision,at FROM skill_evals WHERE skill=? '
                                     'ORDER BY id DESC LIMIT 5', (skill,))})
    elif args.mode == 'generate-eval':
        prop = from_env(store, 'proposer')
        if not prop:
            sys.exit('Configurá VUPPIA_PROPOSER y la API key')
        src = evolve.generate_candidate(store, skill, prop)
        out(evolve.promote_lab(store, skill, src))
    elif args.mode == 'evaluate-file':
        out(evolve.evaluate(store, skill, args.file.read_text()))
    elif args.mode == 'promote-lab':
        out(evolve.promote_lab(store, skill, args.file.read_text()))
    elif args.mode == 'rollback':
        out(evolve.rollback_lab(store, skill, args.sha))
    elif args.mode == 'promote-production':
        if not args.approval_token:
            sys.exit('Falta --approval-token (firmado con la clave del operador). Ver: cli.py keygen')
        out(evolve.promote_production(store, skill, args.sha, args.approval_token))


if __name__ == '__main__':
    main()
