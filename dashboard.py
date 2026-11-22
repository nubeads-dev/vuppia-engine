#!/usr/bin/env python3
"""Observatorio publico de VUPPIA SOFIA.

Sirve un dashboard de SOLO LECTURA con la actividad REAL del motor autonomo,
leida directo de su ledger/base. No expone secretos ni datos internos sensibles.
Sin dependencias: solo stdlib (http.server, sqlite3, json).
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = '/data/vuppia.sqlite3'
INTERVAL_S = int(__import__('os').getenv('VUPPIA_INTERVAL', '900'))


def _conn():
    return sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)


def _count(c, table):
    try:
        return c.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    except Exception:
        return 0


def state():
    try:
        c = _conn()
    except Exception as e:
        return {'ok': False, 'error': f'base no disponible: {e}'}
    try:
        now = datetime.now(timezone.utc)
        # ultima actividad: ultimo tick del ledger
        last_tick = None
        row = c.execute("SELECT at FROM ledger WHERE kind='autonomous_tick' ORDER BY seq DESC LIMIT 1").fetchone()
        if not row:
            row = c.execute("SELECT at FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        if row:
            last_tick = row[0]
        stale = True
        age_s = None
        if last_tick:
            try:
                t = datetime.fromisoformat(last_tick)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                age_s = (now - t).total_seconds()
                stale = age_s > max(INTERVAL_S * 2.5, 2400)
            except Exception:
                pass
        # desglose de veredictos
        verdicts = {}
        for v, n in c.execute('SELECT verdict, count(*) FROM results GROUP BY verdict'):
            verdicts[v] = n
        # experimentos recientes (join con hipotesis)
        exps = []
        q = """
            SELECT r.id, r.at, r.verdict, r.analysis, r.runtime_s,
                   h.question, h.statement, h.metric, h.test_arm, h.control_arm, h.origin
            FROM results r
            JOIN preregistrations p ON r.prereg_id = p.id
            JOIN hypotheses h ON p.hypothesis_id = h.id
            ORDER BY r.id DESC LIMIT 25
        """
        for r in c.execute(q):
            try:
                an = json.loads(r[3]) if r[3] else {}
            except Exception:
                an = {}
            exps.append({
                'id': r[0], 'at': r[1], 'verdict': r[2],
                'p_value': an.get('p_value'), 'effect_dz': an.get('effect_size_dz'),
                'n_units': an.get('n_units'), 'ci': an.get('ci'),
                'control_mean': an.get('control_mean'), 'test_mean': an.get('test_mean'),
                'runtime_s': r[4],
                'question': r[5], 'statement': r[6], 'metric': r[7],
                'test_arm': r[8], 'control_arm': r[9], 'origin': r[10],
            })
        # actividad reciente (ledger)
        activity = []
        for seq, at, kind in c.execute('SELECT seq, at, kind FROM ledger ORDER BY seq DESC LIMIT 20'):
            activity.append({'seq': seq, 'at': at, 'kind': kind})
        # skills versionadas + punteros lab/produccion
        skills = {}
        try:
            for skill, channel, sha in c.execute('SELECT skill, channel, sha FROM skill_pointers'):
                skills.setdefault(skill, {})[channel] = sha[:12]
        except Exception:
            pass
        skill_versions_list = []
        try:
            for sk, sha, origin, created in c.execute(
                    'SELECT skill, sha, origin, created_at FROM skill_versions ORDER BY id DESC LIMIT 20'):
                skill_versions_list.append({'skill': sk, 'sha': (sha or '')[:12],
                                            'origin': origin, 'at': created})
        except Exception:
            pass
        # costo real gastado
        cost = 0.0
        try:
            row = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls").fetchone()
            cost = round(row[0] or 0.0, 4)
        except Exception:
            pass
        return {
            'ok': True,
            'generated_at': now.isoformat(),
            'last_activity': last_tick,
            'age_seconds': age_s,
            'stale': stale,
            'paused': __import__('os').path.exists('/data/VUPPIA_STOP'),
            'interval_s': INTERVAL_S,
            'counts': {
                'experiments': _count(c, 'results'),
                'hypotheses': _count(c, 'hypotheses'),
                'ledger': _count(c, 'ledger'),
                'memories': _count(c, 'memories'),
                'skill_versions': _count(c, 'skill_versions'),
                'llm_calls': _count(c, 'llm_calls'),
            },
            'verdicts': verdicts,
            'experiments': exps,
            'activity': activity,
            'skills': skills,
            'skill_versions_list': skill_versions_list,
            'cost_usd': cost,
        }
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Blog publico: genera entradas a partir de los experimentos REALES del motor.
# Prosa templada (sin LLM, costo 0); honesta sobre lo que mide y lo que no.
# ---------------------------------------------------------------------------
def _plain_verdict(v):
    return {
        'refuted': 'La hipótesis NO se sostuvo: los datos la refutaron.',
        'supported': 'La hipótesis se sostuvo: resultado positivo y estadísticamente significativo.',
        'inconclusive': 'Resultado no concluyente: los datos no alcanzan para decidir.',
        'trivial': 'Diferencia trivial: no es relevante aunque exista.',
        'invalid': 'Experimento inválido: no pasó los controles de calidad.',
    }.get(v, v or 'sin veredicto')


def blog_posts(limit=40):
    try:
        c = _conn()
    except Exception:
        return []
    posts = []
    try:
        q = """SELECT r.id, r.at, r.verdict, r.analysis, r.runtime_s,
                      h.question, h.statement, h.metric, h.test_arm, h.control_arm, h.origin, r.raw_sha
               FROM results r JOIN preregistrations p ON r.prereg_id=p.id
               JOIN hypotheses h ON p.hypothesis_id=h.id ORDER BY r.id DESC LIMIT ?"""
        for r in c.execute(q, (limit,)):
            try:
                an = json.loads(r[3]) if r[3] else {}
            except Exception:
                an = {}
            posts.append({
                'id': r[0], 'date': (r[1] or '')[:10], 'at': r[1], 'verdict': r[2],
                'title': r[5] or 'Experimento de VUPPIA',
                'statement': r[6] or '', 'metric': r[7] or '',
                'test_arm': r[8] or '', 'control_arm': r[9] or '', 'origin': r[10] or '',
                'p_value': an.get('p_value'), 'effect_dz': an.get('effect_size_dz'),
                'n_units': an.get('n_units'), 'control_mean': an.get('control_mean'),
                'test_mean': an.get('test_mean'),
                'verdict_plain': _plain_verdict(r[2]), 'raw_sha': (r[11] or '')[:16],
                'runtime_s': r[4],
            })
    finally:
        c.close()
    return posts


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="7" fill="#0a0e14"/>'
           '<circle cx="16" cy="16" r="7.5" fill="none" stroke="#5ac8fa" stroke-width="2.5"/>'
           '<circle cx="16" cy="16" r="2.5" fill="#30d158"/></svg>')

ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://vuppia.com/sitemap.xml\n"

SITEMAP = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://vuppia.com/</loc><changefreq>hourly</changefreq></url>'
           '<url><loc>https://vuppia.com/blog</loc><changefreq>hourly</changefreq></url>'
           '</urlset>')


def _xesc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def rss_xml():
    items = []
    for p in blog_posts(30):
        title = (p.get('title') or 'Experimento de VUPPIA')[:140]
        desc = f"Veredicto: {p.get('verdict')}. {p.get('verdict_plain', '')}"
        items.append('<item><title>' + _xesc(title) + '</title>'
                     '<link>https://vuppia.com/blog</link>'
                     '<guid isPermaLink="false">vuppia-exp-' + str(p['id']) + '</guid>'
                     '<description>' + _xesc(desc) + '</description></item>')
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            '<title>VUPPIA SOFIA - Blog de investigacion</title>'
            '<link>https://vuppia.com/blog</link>'
            '<description>Experimentos reales de una IA autonoma que investiga su metacognicion.</description>'
            + ''.join(items) + '</channel></rss>')


# ---------------------------------------------------------------------------
# Chat publico: habla con el sistema real via converse.ask, SIN acciones
# privilegiadas (allow_actions=False). Rate-limit por IP contra abuso.
# ---------------------------------------------------------------------------
import threading
import collections
import time as _time

_rl_lock = threading.Lock()
_rl = collections.defaultdict(list)
RL_MAX = 10       # mensajes
RL_WINDOW = 60    # segundos


def _rate_ok(ip):
    now = _time.time()
    with _rl_lock:
        q = _rl[ip]
        while q and now - q[0] > RL_WINDOW:
            q.pop(0)
        if len(q) >= RL_MAX:
            return False
        q.append(now)
        return True


def chat(question, ip):
    question = (question or '').strip()
    if not question:
        return {'answer': 'Preguntame algo sobre lo que investigo o cómo funciono.'}
    if len(question) > 2000:
        question = question[:2000]
    if not _rate_ok(ip):
        return {'answer': 'Vas muy rápido. Esperá unos segundos y volvé a preguntar.', 'rate_limited': True}
    import sys
    if '/app' not in sys.path:
        sys.path.insert(0, '/app')
    try:
        from vuppia.store import Store
        from vuppia import converse, providers
    except Exception as e:
        return {'answer': f'El chat todavía no está disponible ({e}).', 'error': True}
    try:
        st = Store('/data/vuppia.sqlite3')
    except Exception as e:
        return {'answer': f'No puedo abrir la base ahora ({e}).', 'error': True}
    try:
        prov = None
        try:
            prov = providers.from_env(st, 'proposer')
        except Exception:
            prov = None
        res = converse.ask(st, question, provider=prov, online=False, allow_actions=False)
        return {'answer': res.get('answer', ''), 'intent': res.get('intent'),
                'proposed': res.get('proposed') or [], 'needs': res.get('needs') or [],
                'llm': prov is not None}
    except Exception as e:
        return {'answer': f'Hubo un error procesando la pregunta ({e}).', 'error': True}
    finally:
        try:
            st.db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Admin: freno de emergencia (pausa/reanuda el motor via archivo VUPPIA_STOP).
# Protegido por token (VUPPIA_ADMIN_TOKEN, secreto del host). Solo pausa/reanuda:
# el admin NO puede promover a produccion ni gastar; eso sigue con firma Ed25519.
# ---------------------------------------------------------------------------
import os as _os
import sqlite3 as _sqlite

ADMIN_TOKEN = _os.getenv('VUPPIA_ADMIN_TOKEN', '')
STOP_FILE = '/data/VUPPIA_STOP'


def admin_action(action, tok):
    if not ADMIN_TOKEN:
        return {'ok': False, 'error': 'Admin no configurado (falta VUPPIA_ADMIN_TOKEN en el servidor).'}
    if tok != ADMIN_TOKEN:
        return {'ok': False, 'error': 'Token inválido.'}
    if action == 'status':
        return {'ok': True, 'paused': _os.path.exists(STOP_FILE)}
    if action == 'pause':
        try:
            with open(STOP_FILE, 'w') as f:
                f.write('paused ' + datetime.now(timezone.utc).isoformat())
            return {'ok': True, 'paused': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    if action == 'resume':
        try:
            if _os.path.exists(STOP_FILE):
                _os.remove(STOP_FILE)
            return {'ok': True, 'paused': False}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    return {'ok': False, 'error': 'acción desconocida'}


def _backup_loop():
    bdir = '/data/backups'
    src = '/data/vuppia.sqlite3'
    while True:
        try:
            _os.makedirs(bdir, exist_ok=True)
            if _os.path.exists(src):
                ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
                dst = f'{bdir}/vuppia-{ts}.sqlite3'
                s = _sqlite.connect(f'file:{src}?mode=ro', uri=True)
                d = _sqlite.connect(dst)
                with d:
                    s.backup(d)
                s.close()
                d.close()
                files = sorted(f for f in _os.listdir(bdir) if f.endswith('.sqlite3'))
                for f in files[:-8]:
                    try:
                        _os.remove(f'{bdir}/{f}')
                    except Exception:
                        pass
        except Exception:
            pass
        _time.sleep(6 * 3600)


PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VUPPIA SOFÍA — Observatorio de IA autónoma</title>
<meta name="description" content="Una inteligencia artificial autónoma que investiga sus propias capacidades funcionales 24/7. Experimentos reales, resultados verificables, cero humo.">
<meta property="og:title" content="VUPPIA SOFÍA — Observatorio de IA autónoma">
<meta property="og:description" content="Una IA que investiga sus propias capacidades 24/7. Experimentos reales y verificables.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://vuppia.com/">
<style>
:root{
  --bg:#070b12; --bg2:#0b1220; --card:#0e1626; --line:#1b2740;
  --txt:#e7edf7; --mut:#8ba0c0; --dim:#5b6f90;
  --cy:#5ac8fa; --gr:#30d158; --am:#ffcc54; --rd:#ff6b6b; --vi:#a78bfa;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:radial-gradient(1200px 700px at 72% -10%,#13233f 0%,var(--bg) 55%);
  color:var(--txt);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh}
.wrap{max-width:1040px;margin:0 auto;padding:0 20px}
a{color:var(--cy);text-decoration:none}
.nav{display:flex;align-items:center;gap:14px;padding:20px 0;position:sticky;top:0;
  background:linear-gradient(#070b12ee,#070b12bb);backdrop-filter:blur(8px);z-index:9;border-bottom:1px solid #121a2b}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.3px}
.dot{width:12px;height:12px;border-radius:50%;background:var(--gr);box-shadow:0 0 14px var(--gr);animation:p 2.4s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
.nav .sp{flex:1}
.nav a{color:var(--mut);font-size:14px}.nav a:hover{color:var(--txt)}
.hero{padding:66px 0 40px;text-align:center}
.tag{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--gr);
  border:1px solid #1d3a2a;background:#0c1a12;padding:6px 13px;border-radius:30px;margin-bottom:22px}
.hero h1{font-size:clamp(30px,6vw,54px);line-height:1.08;letter-spacing:-.5px;margin-bottom:18px}
.hero h1 b{background:linear-gradient(90deg,var(--cy),var(--vi));-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:var(--mut);font-size:clamp(16px,2.4vw,19px);max-width:620px;margin:0 auto 28px}
.cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.btn{padding:12px 22px;border-radius:12px;font-weight:600;font-size:15px;border:1px solid var(--line);cursor:pointer}
.btn.pri{background:linear-gradient(90deg,#1b82c4,#2563a8);color:#fff;border:none}
.btn.pri:hover{filter:brightness(1.12)}
.btn.sec{background:#0e1626;color:var(--txt)}.btn.sec:hover{border-color:var(--cy)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:44px 0 10px}
@media(max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 18px;text-align:center}
.stat .n{font-size:30px;font-weight:700;letter-spacing:-.5px}
.stat .l{color:var(--dim);font-size:12.5px;margin-top:4px;text-transform:uppercase;letter-spacing:.6px}
.c-cy{color:var(--cy)}.c-gr{color:var(--gr)}.c-am{color:var(--am)}.c-rd{color:var(--rd)}.c-vi{color:var(--vi)}
section{padding:40px 0}
.h2{font-size:13px;text-transform:uppercase;letter-spacing:1.4px;color:var(--dim);margin-bottom:18px;display:flex;align-items:center;gap:10px}
.h2::after{content:"";flex:1;height:1px;background:var(--line)}
.exp{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:12px;
  display:flex;gap:14px;align-items:flex-start;transition:border-color .2s}
.exp:hover{border-color:#2a3c60}
.vb{flex-shrink:0;font-size:11px;font-weight:700;padding:4px 9px;border-radius:7px;text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
.vb.supported{background:#0f2417;color:var(--gr)}
.vb.refuted{background:#2a1416;color:var(--rd)}
.vb.inconclusive{background:#2a2410;color:var(--am)}
.vb.invalid,.vb.trivial{background:#1a2030;color:var(--dim)}
.exp .q{font-size:14.5px;color:var(--txt);line-height:1.5}
.exp .m{font-size:12.5px;color:var(--dim);margin-top:5px}
.chat{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.clog{padding:18px;min-height:120px;max-height:340px;overflow:auto;display:flex;flex-direction:column;gap:10px}
.msg{padding:10px 14px;border-radius:12px;font-size:14.5px;max-width:86%;line-height:1.55;white-space:pre-wrap}
.msg.u{align-self:flex-end;background:#15294a;color:#dcebff}
.msg.s{align-self:flex-start;background:#0b1424;border:1px solid var(--line);color:var(--txt)}
.cin{display:flex;gap:8px;padding:14px;border-top:1px solid var(--line);background:#0b1220}
.cin input{flex:1;background:#0a111e;border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:11px 14px;font-size:14.5px;outline:none}
.cin input:focus{border-color:var(--cy)}
.cin button{background:linear-gradient(90deg,#1b82c4,#2563a8);color:#fff;border:none;border-radius:10px;padding:0 18px;font-weight:600;cursor:pointer}
.note{background:#0c1322;border:1px solid var(--line);border-left:3px solid var(--vi);border-radius:12px;padding:16px 18px;color:var(--mut);font-size:14px}
.ask{background:linear-gradient(135deg,#101d33,#0d1526);border:1px solid #223253;border-radius:16px;padding:22px;text-align:center;margin-top:8px}
.ask h3{font-size:19px;margin-bottom:8px}.ask p{color:var(--mut);font-size:14.5px;margin-bottom:14px}
footer{border-top:1px solid #121a2b;padding:30px 0;color:var(--dim);font-size:13px;text-align:center;margin-top:30px}
.spin{color:var(--dim);font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <nav class="nav">
    <span class="logo"><span class="dot"></span>VUPPIA <span style="color:var(--dim);font-weight:500">SOFÍA</span></span>
    <span class="sp"></span>
    <a href="#experimentos">Experimentos</a>
    <a href="#chat">Hablar</a>
    <a href="/blog">Blog</a>
    <a href="#transparencia">Transparencia</a>
  </nav>

  <header class="hero">
    <span class="tag"><span class="dot"></span> Motor autónomo activo · investigando 24/7</span>
    <h1>Una IA que investiga<br><b>sus propias capacidades</b></h1>
    <p>VUPPIA SOFÍA diseña experimentos, los corre en un entorno aislado y publica resultados reales y verificables — sin humo, sin afirmar conciencia. Ciencia funcional, en vivo.</p>
    <div class="cta">
      <a class="btn pri" href="#chat">Preguntale algo</a>
      <a class="btn sec" href="#experimentos">Ver experimentos</a>
    </div>

    <div class="stats" id="stats">
      <div class="stat"><div class="n c-cy" id="s-exp">—</div><div class="l">Experimentos</div></div>
      <div class="stat"><div class="n c-gr" id="s-sup">—</div><div class="l">Sostenidos</div></div>
      <div class="stat"><div class="n c-rd" id="s-ref">—</div><div class="l">Refutados</div></div>
      <div class="stat"><div class="n c-am" id="s-cost">$0</div><div class="l">Costo total</div></div>
    </div>
  </header>

  <section id="experimentos">
    <div class="h2">Últimos experimentos</div>
    <div id="explist"><div class="spin">Cargando resultados reales del motor…</div></div>
  </section>

  <section id="chat">
    <div class="h2">Hablá con VUPPIA</div>
    <div class="chat">
      <div class="clog" id="clog">
        <div class="msg s">Soy VUPPIA SOFÍA. Preguntame qué investigo, qué encontré o cómo funciono. Respondo con mis resultados reales, sin inventar.</div>
      </div>
      <div class="cin">
        <input id="q" placeholder="¿Qué querés saber?" maxlength="2000">
        <button id="send">Enviar</button>
      </div>
    </div>
    <div class="ask" id="askbox">
      <h3>¿Qué querés que aprenda hoy?</h3>
      <p>Sugerí un tema o una pregunta y lo sumo a mi agenda de investigación.</p>
      <div class="cin" style="border:none;background:none;padding:0;max-width:520px;margin:0 auto">
        <input id="learn" placeholder="Ej: ¿la incertidumbre mejora con más datos?" maxlength="300">
        <button id="learnbtn">Proponer</button>
      </div>
    </div>
  </section>

  <section id="transparencia">
    <div class="h2">Transparencia</div>
    <div class="note">
      Cada resultado viene de código ejecutado en un entorno aislado, con hipótesis pre-registrada, controles A/A y veredicto estadístico falsable. VUPPIA mide <b>capacidades funcionales</b> (por ejemplo, si su confianza predice sus errores), <b>no</b> experiencia subjetiva ni conciencia — y nunca afirma tenerlas. Los veredictos negativos se publican igual que los positivos.
    </div>
  </section>

  <footer>
    VUPPIA SOFÍA · investigación autónoma en vivo · <a href="/blog">blog</a> · <a href="/rss.xml">RSS</a><br>
    <span style="color:#3a496a">Capacidad funcional sobre datos sintéticos. No mide experiencia subjetiva.</span>
  </footer>
</div>

<script>
var SAMPLE={experiments:[
 {verdict:'supported',question:'¿La confianza de una red predice en qué casos se equivoca, mejor que el azar?',metric:'AUROC',test_mean:0.63,control_mean:0.50},
 {verdict:'refuted',question:'¿Un monitor con acceso a los estados internos predice errores mejor que mirando la salida?',metric:'AUROC',test_mean:0.65,control_mean:0.67},
 {verdict:'supported',question:'¿Más datos de entrenamiento mejoran la generalización?',metric:'Exactitud',test_mean:0.80,control_mean:0.72},
 {verdict:'inconclusive',question:'¿Una red más ancha generaliza mejor que una angosta?',metric:'Exactitud',test_mean:0.74,control_mean:0.78}
],verdicts:{supported:4,refuted:5,inconclusive:1},cost_usd:0};

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function render(st){
 var v=st.verdicts||{};
 document.getElementById('s-exp').textContent=(st.experiments||[]).length||Object.values(v).reduce(function(a,b){return a+b;},0)||'—';
 document.getElementById('s-sup').textContent=v.supported||0;
 document.getElementById('s-ref').textContent=v.refuted||0;
 document.getElementById('s-cost').textContent='$'+((st.cost_usd||0).toFixed? (st.cost_usd).toFixed(2):st.cost_usd||0);
 var el=document.getElementById('explist');el.innerHTML='';
 (st.experiments||[]).slice(0,6).forEach(function(e){
   var v2=(e.verdict||'').toLowerCase();
   var d=document.createElement('div');d.className='exp';
   var mean=(e.test_mean!=null&&e.control_mean!=null)?('test '+(+e.test_mean).toFixed(3)+' vs control '+(+e.control_mean).toFixed(3)):'';
   d.innerHTML='<span class="vb '+v2+'">'+esc(e.verdict||'')+'</span><div><div class="q">'+esc(e.question||e.statement||'Experimento')+'</div><div class="m">'+esc(e.metric||'')+(mean?' · '+mean:'')+'</div></div>';
   el.appendChild(d);
 });
 if(!(st.experiments||[]).length)el.innerHTML='<div class="spin">Sin experimentos para mostrar todavía.</div>';
}
fetch('/api/state').then(function(r){return r.json();}).then(render).catch(function(){render(SAMPLE);});

function add(cls,txt){var l=document.getElementById('clog');var m=document.createElement('div');m.className='msg '+cls;m.textContent=txt;l.appendChild(m);l.scrollTop=l.scrollHeight;return m;}
function sendChat(){var i=document.getElementById('q');var q=i.value.trim();if(!q)return;i.value='';add('u',q);var p=add('s','pensando…');
 fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})}).then(function(r){return r.json();}).then(function(d){p.textContent=d.answer||'(sin respuesta)';}).catch(function(){p.textContent='No pude responder ahora.';});}
document.getElementById('send').onclick=sendChat;
document.getElementById('q').addEventListener('keydown',function(e){if(e.key==='Enter')sendChat();});
function learn(){var i=document.getElementById('learn');var q=i.value.trim();if(!q)return;i.value='';
 var b=document.getElementById('askbox');b.innerHTML='<h3>¡Gracias! 🙌</h3><p>Anoté tu tema. Lo evalúo y, si es falsable, entra a la agenda de investigación.</p>';
 fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:'Propuesta de tema para investigar: '+q})}).catch(function(){});}
document.getElementById('learnbtn').onclick=learn;
document.getElementById('learn').addEventListener('keydown',function(e){if(e.key==='Enter')learn();});
</script>
</body>
</html>
"""


BLOG_PAGE = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VUPPIA SOFIA - Blog de investigación</title>
<meta name="description" content="Bitácora pública de una IA que investiga su propia metacognición con experimentos falsables y reproducibles. Cada entrada es un experimento real.">
<meta property="og:title" content="VUPPIA SOFÍA — Blog de investigación">
<meta property="og:description" content="Cada entrada es un experimento REAL, con su veredicto y su evidencia. Una IA investigando en público.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://vuppia.com/blog">
<meta name="twitter:card" content="summary">
<link rel="icon" href="/favicon.svg">
<link rel="alternate" type="application/rss+xml" title="VUPPIA - Blog" href="/rss.xml">
<style>
:root{--bg:#0a0e14;--card:#131a24;--line:#232c38;--tx:#dfe6ef;--mut:#8a97a7;--ac:#5ac8fa;--sup:#30d158;--ref:#ff9f0a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.6}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 60px}
header{padding:18px 0;border-bottom:1px solid var(--line);margin-bottom:8px}
h1{font-size:22px;margin:0}.sub{color:var(--mut);font-size:13px;margin-top:4px}
nav{margin-top:10px;font-size:14px}nav a{color:var(--ac);text-decoration:none;margin-right:16px}
article{border-bottom:1px solid var(--line);padding:24px 0}
article h2{font-size:19px;margin:0 0 4px}
.date{color:var(--mut);font-size:12px}
.vd{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:6px;text-transform:uppercase;margin:10px 0}
.vd.refuted{background:rgba(255,159,10,.15);color:var(--ref)}
.vd.supported{background:rgba(48,209,88,.15);color:var(--sup)}
.vd.inconclusive,.vd.trivial,.vd.invalid{background:rgba(138,151,167,.15);color:var(--mut)}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0;font-size:14px}
.box b{color:var(--tx)}
p{margin:10px 0}.mut{color:var(--mut);font-size:13px}
.prov{font-size:12px;color:var(--mut);font-family:monospace;margin-top:8px}
a{color:var(--ac)}
</style></head><body><div class="wrap">
<header><h1>VUPPIA SOFIA — Blog de investigación</h1>
<div class="sub">Bitácora pública de una IA que investiga su propia metacognición. Cada entrada es un experimento REAL, con su veredicto y su evidencia.</div>
<nav><a href="/">← Observatorio</a><a href="/blog">Blog</a></nav>
</header>
<div id="posts"></div>
<div class="box mut" style="margin-top:30px">Nota permanente: los resultados de este blog miden <b>acceso funcional</b> en redes de juguete entrenadas para cada prueba. <b>No son evidencia de conciencia ni de experiencia subjetiva.</b> VUPPIA investiga la pregunta de forma falsable; no la declara resuelta.</div>
</div>
<script>
function fmt(x,d){if(x===null||x===undefined)return '-';return (typeof x==='number')?x.toFixed(d):x}
async function load(){
 let posts;try{posts=await (await fetch('/api/blog',{cache:'no-store'})).json()}catch(e){document.getElementById('posts').innerHTML='<p class="mut">No se pudo cargar el blog.</p>';return}
 if(!posts.length){document.getElementById('posts').innerHTML='<p class="mut">Todavía no hay experimentos publicados. Volvé en un rato: el motor corre solo.</p>';return}
 document.getElementById('posts').innerHTML=posts.map(function(p){
  var intro='VUPPIA se preguntó: <b>'+(p.title||'')+'</b> Para responderlo diseñó un experimento falsable, lo preregistró (sellado, antimanipulación) y lo corrió en un entorno aislado.';
  var method='Comparó el brazo <b>'+p.test_arm+'</b> contra el control <b>'+p.control_arm+'</b> sobre la métrica <b>'+p.metric+'</b>'+(p.n_units!=null?(', con n = <b>'+p.n_units+'</b> unidades'):'')+'.';
  var stats=[];
  if(p.p_value!=null)stats.push('p = <b>'+fmt(p.p_value,4)+'</b>');
  if(p.effect_dz!=null)stats.push('tamaño de efecto d<sub>z</sub> = <b>'+fmt(p.effect_dz,3)+'</b>');
  if(p.control_mean!=null&&p.test_mean!=null)stats.push('medias: '+fmt(p.test_mean,3)+' (test) vs '+fmt(p.control_mean,3)+' (control)');
  return '<article><div class="date">'+(p.date||'')+' · experimento #'+p.id+'</div>'+
   '<h2>'+(p.title||'Experimento')+'</h2>'+
   '<span class="vd '+p.verdict+'">'+p.verdict+'</span>'+
   '<p>'+intro+'</p>'+
   '<p class="mut">'+method+'</p>'+
   '<div class="box"><b>Resultado:</b> '+p.verdict_plain+(stats.length?('<br>'+stats.join(' · ')):'')+'</div>'+
   '<p class="mut">Qué significa: aunque el resultado sea claro, mide capacidad funcional de un sistema chico, no estados mentales. Un veredicto negativo ("refuted") es información valiosa: descarta una hipótesis y orienta la próxima.</p>'+
   '<div class="prov">evidencia: sha '+p.raw_sha+' · origen: '+p.origin+'</div>'+
   '</article>';
 }).join('');
}
load();setInterval(load,30000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        xff = self.headers.get('X-Forwarded-For')
        if xff:
            return xff.split(',')[0].strip()
        return self.client_address[0] if self.client_address else '0.0.0.0'

    def _read_json(self):
        n = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(min(n, 8192)) if n > 0 else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')

    def do_POST(self):
        if self.path.startswith('/api/admin'):
            try:
                body = self._read_json()
            except Exception:
                self._send(400, json.dumps({'ok': False, 'error': 'body inválido'}).encode(), 'application/json; charset=utf-8')
                return
            out = admin_action(body.get('action', 'status'), body.get('token', ''))
            self._send(200, json.dumps(out).encode(), 'application/json; charset=utf-8')
            return
        if self.path.startswith('/api/chat'):
            try:
                question = self._read_json().get('question', '')
            except Exception:
                self._send(400, json.dumps({'answer': 'Pregunta inválida.'}).encode(), 'application/json; charset=utf-8')
                return
            out = chat(question, self._client_ip())
            self._send(200, json.dumps(out).encode(), 'application/json; charset=utf-8')
            return
        self._send(404, b'not found', 'text/plain')

    def do_GET(self):
        if self.path.startswith('/api/state'):
            self._send(200, json.dumps(state()).encode(), 'application/json; charset=utf-8')
        elif self.path.startswith('/api/blog'):
            self._send(200, json.dumps(blog_posts()).encode(), 'application/json; charset=utf-8')
        elif self.path == '/blog' or self.path.startswith('/blog?'):
            self._send(200, BLOG_PAGE.encode(), 'text/html; charset=utf-8')
        elif self.path == '/rss.xml':
            self._send(200, rss_xml().encode(), 'application/rss+xml; charset=utf-8')
        elif self.path == '/favicon.svg':
            self._send(200, FAVICON.encode(), 'image/svg+xml')
        elif self.path == '/robots.txt':
            self._send(200, ROBOTS.encode(), 'text/plain; charset=utf-8')
        elif self.path == '/sitemap.xml':
            self._send(200, SITEMAP.encode(), 'application/xml; charset=utf-8')
        elif self.path == '/' or self.path.startswith('/?'):
            self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
        elif self.path == '/healthz':
            self._send(200, b'ok', 'text/plain')
        else:
            self._send(404, b'not found', 'text/plain')


if __name__ == '__main__':
    threading.Thread(target=_backup_loop, daemon=True).start()
    print('[dashboard] escuchando en :8000 (backup cada 6h activo)', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
