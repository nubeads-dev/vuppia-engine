#!/usr/bin/env python3
"""Observatorio publico de VUPPIA SOFIA.

Sirve un dashboard de SOLO LECTURA con la actividad REAL del motor autonomo,
leida directo de su ledger/base. No expone secretos ni datos internos sensibles.
Sin dependencias: solo stdlib (http.server, sqlite3, json).
"""
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
    _AR_TZ = ZoneInfo('America/Argentina/Buenos_Aires')
except Exception:                                          # pragma: no cover
    _AR_TZ = timezone(timedelta(hours=-3))


def _ar(ts):
    """Convierte un timestamp UTC de la base a hora argentina (dd/mm/YYYY HH:MM). Nunca lee UTC a ojo."""
    if not ts:
        return ''
    try:
        d = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(_AR_TZ).strftime('%d/%m/%Y %H:%M')
    except Exception:
        return str(ts)[:16]


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
                'id': r[0], 'at': r[1], 'at_ar': _ar(r[1]), 'verdict': r[2],
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
        # cuota del proveedor (headers x-ratelimit-* que guarda providers._persist_quota)
        quota = None
        try:
            qrow = c.execute("SELECT v FROM kv WHERE k='provider_quota'").fetchone()
            if qrow and qrow[0]:
                quota = json.loads(qrow[0])
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
                'articles': _count(c, 'articles'),
            },
            'verdicts': verdicts,
            'experiments': exps,
            'activity': activity,
            'skills': skills,
            'skill_versions_list': skill_versions_list,
            'cost_usd': cost,
            'provider_quota': quota,
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
    has_articles = False
    try:
        c.execute('SELECT 1 FROM articles LIMIT 1')
        has_articles = True
    except Exception:
        has_articles = False
    slug_sel = '(SELECT slug FROM articles a WHERE a.result_id=r.id)' if has_articles else 'NULL'
    try:
        q = ("""SELECT r.id, r.at, r.verdict, r.analysis, r.runtime_s,
                      h.question, h.statement, h.metric, h.test_arm, h.control_arm, h.origin, r.raw_sha,
                      """ + slug_sel + """
               FROM results r JOIN preregistrations p ON r.prereg_id=p.id
               JOIN hypotheses h ON p.hypothesis_id=h.id ORDER BY r.id DESC LIMIT ?""")
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
                'runtime_s': r[4], 'slug': r[12],
            })
    finally:
        c.close()
    return posts


def _md_html(md):
    import re as _re
    import html as _h

    def inline(t):
        t = _h.escape(t)
        t = _re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
        t = _re.sub(r'\*([^*]+)\*', r'<i>\1</i>', t)
        return t
    out, inlist = [], False
    for ln in (md or '').split('\n'):
        s = ln.rstrip()
        if s.startswith('###'):
            if inlist: out.append('</ul>'); inlist = False
            out.append('<h3>' + inline(s.lstrip('#').strip()) + '</h3>')
        elif s.startswith('##') or s.startswith('# '):
            if inlist: out.append('</ul>'); inlist = False
            out.append('<h2>' + inline(s.lstrip('#').strip()) + '</h2>')
        elif s.startswith('- ') or s.startswith('* '):
            if not inlist: out.append('<ul>'); inlist = True
            out.append('<li>' + inline(s[2:]) + '</li>')
        elif not s.strip():
            if inlist: out.append('</ul>'); inlist = False
        else:
            if inlist: out.append('</ul>'); inlist = False
            out.append('<p>' + inline(s) + '</p>')
    if inlist: out.append('</ul>')
    return '\n'.join(out)


def article_list(limit=60):
    try:
        c = _conn()
    except Exception:
        return []
    out = []
    try:
        try:
            rows = c.execute('SELECT a.result_id,a.slug,a.title,a.meta,a.created_at,r.verdict '
                             'FROM articles a JOIN results r ON a.result_id=r.id '
                             'ORDER BY a.result_id DESC LIMIT ?', (limit,)).fetchall()
        except Exception:
            rows = []
        for r in rows:
            out.append({'id': r[0], 'slug': r[1], 'title': r[2], 'meta': r[3],
                        'at_ar': _ar(r[4]), 'verdict': r[5], 'verdict_plain': _plain_verdict(r[5])})
    finally:
        c.close()
    return out


def article_by_slug(slug):
    try:
        c = _conn()
    except Exception:
        return None
    r = None
    try:
        r = c.execute('SELECT a.result_id,a.slug,a.title,a.meta,a.body_md,a.keywords,a.created_at,r.verdict '
                      'FROM articles a JOIN results r ON a.result_id=r.id WHERE a.slug=?', (slug,)).fetchone()
    except Exception:
        r = None
    finally:
        c.close()
    if not r:
        return None
    return {'id': r[0], 'slug': r[1], 'title': r[2], 'meta': r[3], 'body_md': r[4],
            'keywords': r[5], 'at_ar': _ar(r[6]), 'verdict': r[7]}


def article_page(a):
    import html as _h
    url = 'https://vuppia.com/blog/' + a['slug']
    title = _h.escape(a['title']); meta = _h.escape(a['meta'] or '')
    kw = _h.escape(a.get('keywords') or '')
    ld = json.dumps({'@context': 'https://schema.org', '@type': 'Article', 'headline': a['title'],
                     'description': a['meta'], 'keywords': a.get('keywords') or '',
                     'author': {'@type': 'Organization', 'name': 'VUPPIA SOFÍA'},
                     'publisher': {'@type': 'Organization', 'name': 'VUPPIA SOFÍA',
                                   'url': 'https://vuppia.com'},
                     'mainEntityOfPage': url}, ensure_ascii=False)
    body = _md_html(a['body_md'])
    css = ('body{background:#070b12;color:#e7edf7;font:17px/1.75 -apple-system,BlinkMacSystemFont,'
           '"Segoe UI",Roboto,Inter,sans-serif;margin:0}.wrap{max-width:720px;margin:0 auto;padding:0 20px 60px}'
           'a{color:#5ac8fa;text-decoration:none}nav{padding:20px 0;font-size:14px;color:#5b6f90}'
           'h1{font-size:clamp(26px,5vw,38px);line-height:1.2;margin:18px 0 6px}'
           'h2{font-size:21px;margin:30px 0 8px;color:#a78bfa}h3{font-size:17px;margin:22px 0 6px}'
           '.meta{color:#5b6f90;font-size:13.5px;margin-bottom:8px}'
           '.vb{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:7px;'
           'text-transform:uppercase;margin-bottom:18px}.supported{background:#0f2417;color:#30d158}'
           '.refuted{background:#2a1416;color:#ff6b6b}.inconclusive{background:#2a2410;color:#ffcc54}'
           '.trivial,.invalid{background:#1a2030;color:#8ba0c0}'
           'p{color:#c7d3e6}li{color:#c7d3e6;margin:6px 0}ul{padding-left:20px}'
           '.note{background:#0c1322;border-left:3px solid #a78bfa;border-radius:10px;padding:14px 16px;'
           'color:#8ba0c0;font-size:14px;margin:30px 0}footer{border-top:1px solid #121a2b;padding:26px 0;'
           'color:#3a496a;font-size:13px}')
    v = (a.get('verdict') or '').lower()
    return ('<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title} · VUPPIA SOFÍA</title><meta name="description" content="{meta}">'
            f'<meta name="keywords" content="{kw}"><link rel="canonical" href="{url}">'
            '<meta property="og:type" content="article">'
            f'<meta property="og:title" content="{title}"><meta property="og:description" content="{meta}">'
            f'<meta property="og:url" content="{url}"><meta property="og:site_name" content="VUPPIA SOFÍA">'
            '<link rel="icon" href="/favicon.svg">'
            f'<script type="application/ld+json">{ld}</script><style>{css}</style></head><body><div class="wrap">'
            '<nav><a href="/">← Observatorio</a> · <a href="/blog">Blog</a></nav>'
            f'<span class="vb {v}">{_h.escape(a.get("verdict") or "")}</span>'
            f'<h1>{title}</h1><div class="meta">Experimento #{a["id"]} · {_h.escape(a.get("at_ar") or "")} · '
            'VUPPIA SOFÍA</div>'
            f'{body}'
            '<div class="note">Esto mide una capacidad <b>funcional</b> en redes de prueba, con hipótesis '
            'pre-registrada y control A/A. No es evidencia de conciencia ni experiencia subjetiva — VUPPIA '
            'investiga esa pregunta, no la declara resuelta.</div>'
            '<footer>VUPPIA SOFÍA · investigación autónoma en vivo · <a href="/blog">más artículos</a> · '
            '<a href="/rss.xml">RSS</a></footer></div></body></html>')


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="7" fill="#0a0e14"/>'
           '<circle cx="16" cy="16" r="7.5" fill="none" stroke="#5ac8fa" stroke-width="2.5"/>'
           '<circle cx="16" cy="16" r="2.5" fill="#30d158"/></svg>')

ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://vuppia.com/sitemap.xml\n"

def sitemap_xml():
    urls = ['<url><loc>https://vuppia.com/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>',
            '<url><loc>https://vuppia.com/blog</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>']
    try:
        for a in article_list(500):
            urls.append('<url><loc>https://vuppia.com/blog/' + _xesc(a['slug']) +
                        '</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>')
    except Exception:
        pass
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(urls) + '</urlset>')


def _xesc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def rss_xml():
    items = []
    for p in blog_posts(30):
        title = (p.get('title') or 'Experimento de VUPPIA')[:140]
        desc = f"Veredicto: {p.get('verdict')}. {p.get('verdict_plain', '')}"
        link = ('https://vuppia.com/blog/' + _xesc(p['slug'])) if p.get('slug') else 'https://vuppia.com/blog'
        items.append('<item><title>' + _xesc(title) + '</title>'
                     '<link>' + link + '</link>'
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
        # Prioridad de chat: avisa al motor que ceda la cuota de Groq unos segundos.
        try:
            from datetime import timedelta
            until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            with st.tx() as db:
                db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')
                db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('chat_active_until',?)", (until,))
        except Exception:
            pass
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


def propose(topic, ip):
    """Lazo de retroalimentacion: lo que la gente pide aprender entra a la agenda REAL.
    El motor autonomo toma el item activo de mayor prioridad y lo investiga en su proximo tick."""
    topic = (topic or '').strip()
    if not topic:
        return {'ok': False, 'error': 'Decime un tema o pregunta para investigar.'}
    if len(topic) > 300:
        topic = topic[:300]
    if not _rate_ok(ip):
        return {'ok': False, 'rate_limited': True, 'error': 'Vas muy rapido. Espera unos segundos.'}
    import sys
    if '/app' not in sys.path:
        sys.path.insert(0, '/app')
    try:
        from vuppia.store import Store
        from vuppia import agenda
    except Exception as e:
        return {'ok': False, 'error': f'no disponible ({e})'}
    try:
        st = Store('/data/vuppia.sqlite3')
    except Exception as e:
        return {'ok': False, 'error': f'no puedo abrir la base ({e})'}
    try:
        row = st.one("SELECT COUNT(*) FROM agenda WHERE status='active' AND origin='user'")
        n_active = row[0] if row else 0
        status = 'active' if n_active < 6 else 'proposed'
        short = topic if len(topic) <= 80 else topic[:77] + '...'
        aid = agenda.add(st, short, topic, status=status, priority=2.0, origin='user')
        if status == 'active':
            msg = 'Lo sumé a mi agenda y lo voy a investigar en una de mis próximas corridas.'
        else:
            msg = 'Lo sumé a mi agenda como propuesta (ya tengo varias en curso); lo tomo en cuanto se libere un lugar.'
        return {'ok': True, 'id': aid, 'status': status, 'topic': short, 'message': msg}
    except Exception as e:
        return {'ok': False, 'error': f'no pude anotarlo ({e})'}
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


def _admin_store():
    import sys
    if '/app' not in sys.path:
        sys.path.insert(0, '/app')
    from vuppia.store import Store
    from vuppia import agenda
    return Store('/data/vuppia.sqlite3'), agenda


def admin_action(action, tok, args=None):
    args = args or {}
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

    # --- Agenda: leer y gobernar (prioridad / suspender / activar / aprobar) ---
    if action in ('agenda_list', 'agenda_priority', 'agenda_status'):
        st = None
        try:
            st, agenda = _admin_store()
        except Exception as e:
            return {'ok': False, 'error': f'no puedo abrir la base ({e})'}
        try:
            if action == 'agenda_list':
                items = agenda.listing(st, limit=200)
                order = {'active': 0, 'proposed': 1, 'suspended': 2, 'done': 3}
                items.sort(key=lambda x: (order.get(x['status'], 9), -float(x['priority'] or 0), -x['id']))
                counts = {}
                for it in items:
                    counts[it['status']] = counts.get(it['status'], 0) + 1
                return {'ok': True, 'items': items, 'counts': counts}
            try:
                aid = int(args.get('id'))
            except Exception:
                return {'ok': False, 'error': 'id inválido'}
            if action == 'agenda_priority':
                try:
                    pr = float(args.get('priority'))
                except Exception:
                    return {'ok': False, 'error': 'prioridad inválida'}
                pr = max(0.0, min(10.0, pr))
                ok = agenda.set_priority(st, aid, pr)
                return {'ok': bool(ok), 'id': aid, 'priority': pr}
            if action == 'agenda_status':
                ns = args.get('status', '')
                if ns not in ('active', 'suspended', 'proposed', 'done'):
                    return {'ok': False, 'error': 'estado inválido'}
                ok = agenda.set_status(st, aid, ns)
                return {'ok': bool(ok), 'id': aid, 'status': ns}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            if st is not None:
                try:
                    st.db.close()
                except Exception:
                    pass

    # --- Evolución de código: ver candidatos y promover a producción CON FIRMA ---
    if action in ('evo_list', 'evo_diff'):
        try:
            c = _conn()
        except Exception as e:
            return {'ok': False, 'error': f'base no disponible ({e})'}
        try:
            skills = ['hypothesis', 'memory_rank']
            ptr = {}
            try:
                for sk, ch, sha in c.execute(
                        "SELECT skill,channel,sha FROM skill_pointers WHERE channel IN ('lab','production')"):
                    ptr[(sk, ch)] = sha
            except Exception:
                pass
            if action == 'evo_diff':
                sk = args.get('skill', '')
                if sk not in skills:
                    return {'ok': False, 'error': 'skill desconocida'}
                def _src(sha):
                    if not sha:
                        return None
                    r = c.execute('SELECT source FROM skill_versions WHERE skill=? AND sha=?', (sk, sha)).fetchone()
                    return r[0] if r else None
                lab = ptr.get((sk, 'lab')); prod = ptr.get((sk, 'production'))
                base = _src(prod)
                if base is None:
                    try:
                        import sys as _s
                        if '/app' not in _s.path:
                            _s.path.insert(0, '/app')
                        from vuppia import evolve as _ev
                        base = _ev.seed_source(sk)
                        base_label = 'semilla'
                    except Exception:
                        base = ''; base_label = 'semilla'
                else:
                    base_label = 'producción'
                return {'ok': True, 'skill': sk, 'lab_source': _src(lab) or '',
                        'base_source': base or '', 'base_label': base_label}
            out = []
            for sk in skills:
                lab = ptr.get((sk, 'lab')); prod = ptr.get((sk, 'production'))
                ev = None
                if lab:
                    r = c.execute('SELECT decision,analysis,at FROM skill_evals WHERE skill=? AND candidate_sha=? '
                                  'ORDER BY id DESC LIMIT 1', (sk, lab)).fetchone()
                    if r:
                        try:
                            an = json.loads(r[1]) if r[1] else {}
                        except Exception:
                            an = {}
                        ev = {'decision': r[0], 'at_ar': _ar(r[2]),
                              'candidate_mean': an.get('candidate_mean'),
                              'incumbent_mean': an.get('incumbent_mean'),
                              'worst_regime_delta': an.get('worst_regime_delta')}
                origin = created = None
                if lab:
                    rv = c.execute('SELECT origin,created_at FROM skill_versions WHERE skill=? AND sha=?',
                                   (sk, lab)).fetchone()
                    if rv:
                        origin, created = rv[0], _ar(rv[1])
                out.append({'skill': sk, 'lab_sha': lab, 'prod_sha': prod,
                            'promotable': bool(lab and lab != prod), 'eval': ev,
                            'origin': origin, 'created_at': created})
            pubkey = bool(_os.getenv('VUPPIA_APPROVAL_PUBKEY', '').strip())
            return {'ok': True, 'skills': out, 'approval_configured': pubkey}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            try:
                c.close()
            except Exception:
                pass

    if action == 'promote':
        sk = args.get('skill', ''); sha = (args.get('sha', '') or '').strip()
        token = (args.get('token_firma', '') or '').strip()
        if not sk or not sha or not token:
            return {'ok': False, 'error': 'faltan skill, sha o token de firma'}
        st = None
        try:
            st, _ = _admin_store()
            import sys as _s
            if '/app' not in _s.path:
                _s.path.insert(0, '/app')
            from vuppia import evolve as _ev
            res = _ev.promote_production(st, sk, sha, token)
            return {'ok': True, **res}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        finally:
            if st is not None:
                try:
                    st.db.close()
                except Exception:
                    pass

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
html,body{overflow-x:hidden;max-width:100%}
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
.chat{background:var(--card);border:1px solid var(--line);border-radius:18px}
.clog{padding:22px;min-height:70px;display:flex;flex-direction:column;gap:16px}
.msg{font-size:15.5px;line-height:1.68;white-space:pre-wrap;max-width:100%;word-wrap:break-word}
.msg.u{align-self:flex-end;background:#15294a;color:#dcebff;padding:12px 16px;border-radius:14px;max-width:85%}
.msg.s{align-self:stretch;color:var(--txt);padding:14px 16px;background:#0b142466;border-left:2px solid var(--cy);border-radius:0 12px 12px 0}
.mbody{white-space:pre-wrap;word-wrap:break-word}
.acts{margin-top:10px;display:flex;gap:8px}
.copybtn{background:#0e1626;border:1px solid var(--line);color:var(--mut);font-size:12.5px;padding:5px 11px;border-radius:8px;cursor:pointer;transition:all .15s}
.copybtn:hover{border-color:var(--cy);color:var(--txt)}
.cin{display:flex;gap:10px;padding:16px;border-top:1px solid var(--line)}
.cin input{flex:1;background:#0a111e;border:1px solid var(--line);color:var(--txt);border-radius:12px;padding:14px 16px;font-size:15.5px;outline:none}
.cin input:focus{border-color:var(--cy)}
.cin button{background:linear-gradient(90deg,#1b82c4,#2563a8);color:#fff;border:none;border-radius:12px;padding:0 22px;font-weight:600;font-size:15px;cursor:pointer}
.note{background:#0c1322;border:1px solid var(--line);border-left:3px solid var(--vi);border-radius:12px;padding:16px 18px;color:var(--mut);font-size:14px}
.qmn{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.qmn{grid-template-columns:1fr}}
@media(max-width:560px){
  .wrap{padding:0 14px}
  .nav{gap:10px;flex-wrap:wrap;padding:14px 0}
  .nav .sp{flex-basis:100%;order:3;height:0}
  .nav a{font-size:13px}
  .hero{padding:40px 0 26px}
  .tag{font-size:11.5px;padding:5px 11px}
  .stats{gap:10px;margin:30px 0 6px}
  .stat{padding:16px 12px}
  .stat .n{font-size:24px}
  section{padding:30px 0}
  .msg{font-size:15px}
  .msg.u{max-width:92%}
  .cin{padding:12px;gap:8px}
  .cin input{padding:12px 13px;font-size:15px;min-width:0}
  .cin button{padding:0 16px}
  .exp{padding:14px}
  .exp .q{font-size:14px}
  .btn{padding:11px 18px;font-size:14.5px}
}
.qmn-col{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.qmn-col.ok{border-left:3px solid var(--gr)}
.qmn-col.no{border-left:3px solid var(--rd)}
.qmn-t{font-weight:700;font-size:14.5px;margin-bottom:10px}
.qmn-col.ok .qmn-t{color:var(--gr)}
.qmn-col.no .qmn-t{color:var(--rd)}
.qmn-col ul{margin:0;padding-left:18px;color:var(--mut);font-size:13.5px;line-height:1.55}
.qmn-col li{margin:6px 0}
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

  <section id="experimentos">
    <div class="h2">Últimos experimentos</div>
    <div id="explist"><div class="spin">Cargando resultados reales del motor…</div></div>
  </section>

  <section id="transparencia">
    <div class="h2">Qué medimos y qué no</div>
    <div class="qmn">
      <div class="qmn-col ok">
        <div class="qmn-t">✓ Qué SÍ medimos</div>
        <ul>
          <li>Capacidades <b>funcionales</b> en redes de prueba: si su confianza predice sus errores, si calibra bien, si generaliza.</li>
          <li>Cada resultado: código ejecutado en entorno aislado, hipótesis pre-registrada, control A/A y veredicto estadístico falsable.</li>
          <li>Los resultados <b>negativos</b> se publican igual que los positivos.</li>
        </ul>
      </div>
      <div class="qmn-col no">
        <div class="qmn-t">✗ Qué NO afirmamos</div>
        <ul>
          <li>Que VUPPIA tenga <b>conciencia</b> o experiencia subjetiva. No hay evidencia de eso y no lo declaramos.</li>
          <li>Capacidad funcional <b>no es</b> lo mismo que experiencia. No las confundimos.</li>
          <li>La conciencia es la <b>pregunta abierta</b> que investigamos con rigor, no una meta alcanzada.</li>
        </ul>
      </div>
    </div>
    <div class="note" style="margin-top:14px">Esta honestidad es el punto: preferimos admitir la distancia entre lo que medimos y la gran pregunta, antes que venderte humo.</div>
  </section>

  <footer>
    VUPPIA SOFÍA · investigación autónoma en vivo · <a href="/blog">blog</a> · <a href="/rss.xml">RSS</a><br>
    <span style="color:#3a496a">Capacidad funcional sobre datos sintéticos. No mide experiencia subjetiva.</span>
  </footer>
</div>

<script>
var SAMPLE={experiments:[
 {id:47,at_ar:'22/11/2026 10:14',verdict:'supported',question:'¿La confianza de una red predice en qué casos se equivoca, mejor que el azar?',metric:'AUROC',test_mean:0.63,control_mean:0.50},
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
   var meta=(e.id?('#'+e.id):'')+(e.at_ar?(' · '+esc(e.at_ar)):'');
   d.innerHTML='<span class="vb '+v2+'">'+esc(e.verdict||'')+'</span><div><div class="q">'+esc(e.question||e.statement||'Experimento')+'</div><div class="m">'+(meta?meta+'<br>':'')+esc(e.metric||'')+(mean?' · '+mean:'')+'</div></div>';
   el.appendChild(d);
 });
 if(!(st.experiments||[]).length)el.innerHTML='<div class="spin">Sin experimentos para mostrar todavía.</div>';
}
fetch('/api/state').then(function(r){return r.json();}).then(render).catch(function(){render(SAMPLE);});

function add(cls,txt){var l=document.getElementById('clog');var m=document.createElement('div');m.className='msg '+cls;var b=document.createElement('div');b.className='mbody';b.textContent=txt;m.appendChild(b);m._body=b;l.appendChild(m);m.scrollIntoView({behavior:'smooth',block:'nearest'});return m;}
function addCopy(m){if(m._done)return;m._done=1;var acts=document.createElement('div');acts.className='acts';var btn=document.createElement('button');btn.className='copybtn';btn.textContent='Copiar';btn.onclick=function(){var t=m._body.textContent;navigator.clipboard.writeText(t).then(function(){btn.textContent='Copiado ✓';setTimeout(function(){btn.textContent='Copiar';},1500);});};acts.appendChild(btn);m.appendChild(acts);}
function sendChat(){var i=document.getElementById('q');var q=i.value.trim();if(!q)return;i.value='';add('u',q);var p=add('s','pensando…');
 fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})}).then(function(r){return r.json();}).then(function(d){p._body.textContent=d.answer||'(sin respuesta)';addCopy(p);p.scrollIntoView({behavior:'smooth',block:'nearest'});}).catch(function(){p._body.textContent='No pude responder ahora.';});}
document.getElementById('send').onclick=sendChat;
document.getElementById('q').addEventListener('keydown',function(e){if(e.key==='Enter')sendChat();});
function learn(){var i=document.getElementById('learn');var q=i.value.trim();if(!q)return;i.value='';
 var b=document.getElementById('askbox');b.innerHTML='<h3>Anotando… ⏳</h3>';
 fetch('/api/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic:q})}).then(function(r){return r.json();}).then(function(d){b.innerHTML='<h3>'+(d.ok?'¡Sumado a mi agenda! 🧠':'Uy')+'</h3><p>'+(d.message||d.error||'')+(d.ok&&d.id?(' (id '+d.id+')'):'')+'</p>';}).catch(function(){b.innerHTML='<h3>Uy</h3><p>No pude anotarlo ahora.</p>';});}
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
  var head=p.slug?('<a href="/blog/'+p.slug+'" style="color:inherit;text-decoration:none">'+(p.title||'Experimento')+'</a>'):(p.title||'Experimento');
  var more=p.slug?('<p><a href="/blog/'+p.slug+'" style="color:#5ac8fa;font-weight:600">Leer el artículo completo →</a></p>'):'';
  return '<article><div class="date">'+(p.date||'')+' · experimento #'+p.id+'</div>'+
   '<h2>'+head+'</h2>'+
   '<span class="vd '+p.verdict+'">'+p.verdict+'</span>'+
   '<p>'+intro+'</p>'+
   '<p class="mut">'+method+'</p>'+
   '<div class="box"><b>Resultado:</b> '+p.verdict_plain+(stats.length?('<br>'+stats.join(' · ')):'')+'</div>'+
   more+
   '<p class="mut">Qué significa: aunque el resultado sea claro, mide capacidad funcional de un sistema chico, no estados mentales. Un veredicto negativo ("refuted") es información valiosa: descarta una hipótesis y orienta la próxima.</p>'+
   '<div class="prov">evidencia: sha '+p.raw_sha+' · origen: '+p.origin+'</div>'+
   '</article>';
 }).join('');
}
load();setInterval(load,30000);
</script></body></html>"""


PANEL_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Panel · VUPPIA SOFÍA</title>
<style>
*{box-sizing:border-box}
body{background:#070b12;color:#e7edf7;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;margin:0}
.wrap{max-width:860px;margin:0 auto;padding:22px 16px 70px}
a{color:#5ac8fa;text-decoration:none}
h1{font-size:22px;margin:4px 0 2px}.sub{color:#5b6f90;font-size:13px;margin-bottom:18px}
.card{background:#0c1322;border:1px solid #16203400;border:1px solid #121a2b;border-radius:14px;padding:16px;margin:14px 0}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
label{font-size:13px;color:#8ba0c0}
input{background:#070b12;border:1px solid #1d2a44;color:#e7edf7;border-radius:9px;padding:9px 11px;font-size:14px;width:100%}
button{background:#15203a;border:1px solid #243457;color:#e7edf7;border-radius:9px;padding:8px 13px;font-size:13px;cursor:pointer;font-weight:600}
button:hover{background:#1d2c4c}button:disabled{opacity:.4;cursor:default}
button.p{background:#153025;border-color:#1f5138;color:#49e08a}
button.d{background:#2a1416;border-color:#5a2a2e;color:#ff8a8a}
button.g{background:#241a33;border-color:#4a3570;color:#c4a9ff}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.kpi{background:#070b12;border:1px solid #121a2b;border-radius:11px;padding:11px}
.kpi .n{font-size:21px;font-weight:700}.kpi .l{font-size:11px;color:#5b6f90;text-transform:uppercase;letter-spacing:.4px}
.big{font-size:14px;padding:12px 18px}
.pill{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:6px;text-transform:uppercase;letter-spacing:.3px}
.active{background:#0f2417;color:#30d158}.proposed{background:#241a33;color:#c4a9ff}.suspended{background:#2a2410;color:#ffcc54}.done{background:#111a2b;color:#5b6f90}
.user{background:#102030;color:#5ac8fa}.self{background:#1a1030;color:#b79bff}
.it{border-top:1px solid #121a2b;padding:12px 0}
.it:first-child{border-top:none}
.it .tp{font-weight:600;margin:3px 0}
.it .q{color:#8ba0c0;font-size:13px}
.pr{font-variant-numeric:tabular-nums;min-width:34px;text-align:center;font-weight:700}
.mut{color:#5b6f90;font-size:13px}
.hid{display:none}
.tag{font-size:11px;color:#5b6f90}
.note{background:#0c1322;border-left:3px solid #a78bfa;border-radius:10px;padding:12px 14px;color:#8ba0c0;font-size:13px;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
<div class="row" style="justify-content:space-between">
  <div><h1>Panel de VUPPIA</h1><div class="sub">Gobernás el motor: freno, agenda y prioridades. · <a href="/">ver sitio público</a></div></div>
</div>

<div id="auth" class="card">
  <label>Token de administración</label>
  <div class="row" style="margin-top:8px">
    <input id="tok" type="password" placeholder="pegá tu token" autocomplete="off" style="flex:1;min-width:200px">
    <button class="p" onclick="saveTok()">Entrar</button>
  </div>
  <div id="autherr" class="mut" style="margin-top:8px"></div>
</div>

<div id="app" class="hid">
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div><b>Estado del motor</b> <span id="engine" class="pill"></span></div>
      <div class="row">
        <button id="pausebtn" class="d big" onclick="togglePause()">Pausar</button>
        <button onclick="refresh()">↻</button>
      </div>
    </div>
    <div class="kpis" style="margin-top:14px">
      <div class="kpi"><div id="k_exp" class="n">–</div><div class="l">Experimentos</div></div>
      <div class="kpi"><div id="k_art" class="n">–</div><div class="l">Artículos</div></div>
      <div class="kpi"><div id="k_cost" class="n">–</div><div class="l">Costo hoy</div></div>
      <div class="kpi"><div id="k_last" class="n">–</div><div class="l">Último tick</div></div>
    </div>
    <div id="verdicts" class="mut" style="margin-top:10px"></div>
    <div id="quota" style="margin-top:12px"></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <b>Agenda de investigación</b>
      <span id="acounts" class="tag"></span>
    </div>
    <div id="agenda" style="margin-top:6px"><div class="mut">Cargando…</div></div>
    <div class="note">Subir prioridad empuja un tema al frente (el motor toma el activo de mayor prioridad en cada corrida). <b>Aprobar</b> pasa una propuesta a activa. <b>Suspender</b> la saca de la cola sin borrarla.</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <b>Evolución de código → producción</b>
      <span id="evostate" class="tag"></span>
    </div>
    <div id="evo" style="margin-top:6px"><div class="mut">Cargando…</div></div>
    <div class="note">VUPPIA reescribe estas funciones y las evalúa sola en el lab (sandbox, control A/A). <b>Nada pasa a producción sin tu firma.</b> Firmás en tu máquina (la clave privada nunca toca el servidor), pegás el token acá y recién ahí el código aprobado entra a la versión viva. Es de un solo uso y vence.</div>
  </div>
</div>

<script>
var TOK = localStorage.getItem('vp_token') || '';
function api(action, args){
  return fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:action,token:TOK,args:args||{}})}).then(function(r){return r.json()});
}
function saveTok(){
  TOK=document.getElementById('tok').value.trim();
  if(!TOK)return;
  api('status').then(function(d){
    if(d.ok){localStorage.setItem('vp_token',TOK);document.getElementById('auth').classList.add('hid');
      document.getElementById('app').classList.remove('hid');refresh();if(!window._vpiv){window._vpiv=setInterval(refresh,20000)}}
    else{document.getElementById('autherr').textContent=d.error||'Token inválido.'}
  }).catch(function(){document.getElementById('autherr').textContent='No pude conectar.'});
}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function ago(sec){if(sec==null)return '–';sec=Math.floor(sec);if(sec<60)return 'hace '+sec+'s';if(sec<3600)return 'hace '+Math.floor(sec/60)+'m';return 'hace '+Math.floor(sec/3600)+'h'}
var PAUSED=false;
function setEngine(st){
  PAUSED=!!st.paused;
  var e=document.getElementById('engine');
  e.textContent=PAUSED?'PAUSADO':(st.stale?'INACTIVO':'CORRIENDO');
  e.className='pill '+(PAUSED?'suspended':(st.stale?'done':'active'));
  var b=document.getElementById('pausebtn');
  b.textContent=PAUSED?'Reanudar':'Pausar';b.className=(PAUSED?'p':'d')+' big';
  var c=st.counts||{};
  document.getElementById('k_exp').textContent=(c.experiments!=null?c.experiments:'–');
  document.getElementById('k_art').textContent=(c.articles!=null?c.articles:'0');
  document.getElementById('k_cost').textContent='$'+(Number(st.cost_usd||0).toFixed(2));
  document.getElementById('k_last').textContent=ago(st.age_seconds);
  var v=st.verdicts||{};var parts=Object.keys(v).map(function(k){return k+': '+v[k]});
  document.getElementById('verdicts').textContent=parts.length?('Veredictos — '+parts.join(' · ')):'';
  renderQuota(st.provider_quota);
}
function qbar(rem,lim){
  rem=Number(rem);lim=Number(lim);
  if(!isFinite(rem)||!isFinite(lim)||lim<=0)return '';
  var pct=Math.max(0,Math.min(100,100*rem/lim));
  var col=pct>40?'#30d158':(pct>15?'#ffcc54':'#ff6b6b');
  return '<div style="background:#070b12;border-radius:6px;height:7px;overflow:hidden;margin-top:4px">'+
    '<div style="height:100%;width:'+pct.toFixed(0)+'%;background:'+col+'"></div></div>';
}
function renderQuota(q){
  var box=document.getElementById('quota');
  if(!q){box.innerHTML='<div class="mut">Cuota del modelo: todavía sin datos (se llena en la próxima llamada a Groq).</div>';return}
  var prov=esc((q.provider||'groq')+(q.model?(' · '+q.model):''));
  var ra=q.retry_after?(' <span class="pill suspended">429 · reintentar en '+esc(q.retry_after)+'s</span>'):'';
  var html='<div class="mut" style="margin-bottom:6px"><b style="color:#e7edf7">Cuota del modelo</b> · '+prov+ra+'</div>';
  html+='<div class="kpis">';
  if(q.remaining_requests!=null){
    html+='<div class="kpi"><div class="l">Requests restantes</div><div class="n" style="font-size:16px">'+esc(q.remaining_requests)+(q.limit_requests?(' / '+esc(q.limit_requests)):'')+'</div>'+qbar(q.remaining_requests,q.limit_requests)+(q.reset_requests?('<div class="l" style="margin-top:3px">reset en '+esc(q.reset_requests)+'</div>'):'')+'</div>';
  }
  if(q.remaining_tokens!=null){
    html+='<div class="kpi"><div class="l">Tokens restantes</div><div class="n" style="font-size:16px">'+esc(q.remaining_tokens)+(q.limit_tokens?(' / '+esc(q.limit_tokens)):'')+'</div>'+qbar(q.remaining_tokens,q.limit_tokens)+(q.reset_tokens?('<div class="l" style="margin-top:3px">reset en '+esc(q.reset_tokens)+'</div>'):'')+'</div>';
  }
  html+='</div>';
  box.innerHTML=html;
}
function refresh(){
  fetch('/api/state',{cache:'no-store'}).then(function(r){return r.json()}).then(setEngine).catch(function(){});
  loadAgenda();
  loadEvo();
}
function fnum(x){return (x==null)?'–':Number(x).toFixed(4)}
function loadEvo(){
  api('evo_list').then(function(d){
    var box=document.getElementById('evo');
    if(!d.ok){box.innerHTML='<div class="mut">'+esc(d.error||'error')+'</div>';return}
    document.getElementById('evostate').textContent=d.approval_configured?'autoridad de firma: configurada':'autoridad de firma: FALTA pubkey en servidor';
    box.innerHTML=(d.skills||[]).map(function(s){
      var prod=s.prod_sha?('<code>'+esc(s.prod_sha.slice(0,12))+'</code>'):'<span class="mut">semilla (sin promover)</span>';
      var lab=s.lab_sha?('<code>'+esc(s.lab_sha.slice(0,12))+'</code>'):'<span class="mut">—</span>';
      var ev=s.eval;var evline='';
      if(ev){
        var dec=ev.decision==='improved'?'<span class="pill active">mejora</span>':'<span class="pill done">sin mejora</span>';
        evline='<div class="mut" style="margin-top:4px">'+dec+' · candidato '+fnum(ev.candidate_mean)+' vs incumbente '+fnum(ev.incumbent_mean)+' · peor régimen Δ '+fnum(ev.worst_regime_delta)+' · '+esc(ev.at_ar||'')+'</div>';
      }
      var actions='<div class="row" style="margin-top:8px"><button onclick="evoDiff(\''+s.skill+'\')">Ver código</button>';
      if(s.promotable)actions+=' <button class="g" onclick="toggleSign(\''+s.skill+'\',\''+esc(s.lab_sha)+'\')">Promover a producción…</button>';
      actions+='</div>';
      var signbox='<div id="sign_'+s.skill+'" class="hid" style="margin-top:10px;border-top:1px solid #121a2b;padding-top:10px">'+
        '<div class="mut">1) Firmá en tu máquina (donde está tu clave privada):</div>'+
        '<input readonly onclick="this.select()" style="margin:6px 0;font-family:monospace;font-size:12px" '+
        'value="python3 -m vuppia.approval sign --skill '+s.skill+' --sha '+esc(s.lab_sha||'')+' --ttl 3600">'+
        '<div class="mut">2) Pegá el token firmado:</div>'+
        '<input id="tok_'+s.skill+'" placeholder="token de aprobación" style="margin:6px 0">'+
        '<button class="p" onclick="promote(\''+s.skill+'\',\''+esc(s.lab_sha)+'\')">Promover a producción</button>'+
        '<div id="promsg_'+s.skill+'" class="mut" style="margin-top:6px"></div></div>';
      var diffbox='<pre id="diff_'+s.skill+'" class="hid" style="margin-top:8px;max-height:300px;overflow:auto;background:#070b12;border:1px solid #121a2b;border-radius:10px;padding:10px;font-size:12px;white-space:pre-wrap"></pre>';
      return '<div class="it"><div class="tp">'+esc(s.skill)+'</div>'+
        '<div class="mut">producción: '+prod+' · lab: '+lab+(s.promotable?' <span class="pill proposed">candidato sin aprobar</span>':' <span class="pill done">al día</span>')+'</div>'+
        evline+actions+signbox+diffbox+'</div>';
    }).join('');
  }).catch(function(){document.getElementById('evo').innerHTML='<div class="mut">No pude cargar la evolución.</div>'});
}
function toggleSign(sk){var e=document.getElementById('sign_'+sk);e.classList.toggle('hid')}
function evoDiff(sk){
  var pre=document.getElementById('diff_'+sk);
  if(!pre.classList.contains('hid')){pre.classList.add('hid');return}
  pre.classList.remove('hid');pre.textContent='Cargando…';
  api('evo_diff',{skill:sk}).then(function(d){
    if(!d.ok){pre.textContent=d.error||'error';return}
    pre.textContent='=== VERSIÓN LAB (candidato) ===\n'+(d.lab_source||'(sin código)')+'\n\n=== BASE EN VIVO ('+(d.base_label||'')+') ===\n'+(d.base_source||'(semilla)');
  }).catch(function(){pre.textContent='error al cargar'});
}
function promote(sk,sha){
  var tok=(document.getElementById('tok_'+sk).value||'').trim();
  var msg=document.getElementById('promsg_'+sk);
  if(!tok){msg.textContent='Pegá el token firmado primero.';return}
  msg.textContent='Verificando firma…';
  api('promote',{skill:sk,sha:sha,token_firma:tok}).then(function(d){
    if(d.ok){msg.innerHTML='<span style="color:#49e08a">✓ Promovido a producción ('+esc((d.promoted_to_production||'').slice(0,12))+'). Ya es la versión viva.</span>';loadEvo()}
    else{msg.innerHTML='<span style="color:#ff8a8a">'+esc(d.error||'no se pudo')+'</span>'}
  }).catch(function(){msg.textContent='error de red'});
}
function togglePause(){
  api(PAUSED?'resume':'pause').then(function(d){if(d.ok)refresh();else alert(d.error||'error')});
}
function loadAgenda(){
  api('agenda_list').then(function(d){
    if(!d.ok){document.getElementById('agenda').innerHTML='<div class="mut">'+esc(d.error||'error')+'</div>';return}
    var c=d.counts||{};
    document.getElementById('acounts').textContent=['active','proposed','suspended','done'].filter(function(k){return c[k]}).map(function(k){return c[k]+' '+k}).join(' · ');
    if(!d.items.length){document.getElementById('agenda').innerHTML='<div class="mut">La agenda está vacía.</div>';return}
    document.getElementById('agenda').innerHTML=d.items.map(function(it){
      var ob='<span class="pill '+(it.origin==='user'?'user':'self')+'">'+(it.origin==='user'?'vos':'ella')+'</span>';
      var sb='<span class="pill '+it.status+'">'+it.status+'</span>';
      var ctl='';
      ctl+='<button onclick="prio('+it.id+','+(Number(it.priority)-0.5)+')">−</button>';
      ctl+='<span class="pr">'+Number(it.priority).toFixed(1)+'</span>';
      ctl+='<button onclick="prio('+it.id+','+(Number(it.priority)+0.5)+')">+</button>';
      if(it.status==='proposed')ctl+=' <button class="p" onclick="stat('+it.id+',\'active\')">Aprobar</button>';
      if(it.status==='active')ctl+=' <button class="d" onclick="stat('+it.id+',\'suspended\')">Suspender</button>';
      if(it.status==='suspended')ctl+=' <button class="g" onclick="stat('+it.id+',\'active\')">Activar</button>';
      if(it.status==='done')ctl+=' <button class="g" onclick="stat('+it.id+',\'active\')">Reabrir</button>';
      return '<div class="it"><div class="row" style="justify-content:space-between">'+
        '<div style="flex:1;min-width:200px"><div>'+sb+' '+ob+' <span class="tag">#'+it.id+'</span></div>'+
        '<div class="tp">'+esc(it.topic)+'</div>'+(it.question&&it.question!==it.topic?('<div class="q">'+esc(it.question)+'</div>'):'')+'</div>'+
        '<div class="row">'+ctl+'</div></div></div>';
    }).join('');
  }).catch(function(){document.getElementById('agenda').innerHTML='<div class="mut">No pude cargar la agenda.</div>'});
}
function prio(id,p){if(p<0)p=0;api('agenda_priority',{id:id,priority:p}).then(function(d){if(d.ok)loadAgenda();else alert(d.error||'error')})}
function stat(id,s){api('agenda_status',{id:id,status:s}).then(function(d){if(d.ok)loadAgenda();else alert(d.error||'error')})}
if(TOK){api('status').then(function(d){if(d.ok){document.getElementById('auth').classList.add('hid');document.getElementById('app').classList.remove('hid');refresh();setInterval(refresh,20000)}})}
</script>
</body></html>"""


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
            out = admin_action(body.get('action', 'status'), body.get('token', ''), body.get('args') or {})
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
        if self.path.startswith('/api/propose'):
            try:
                topic = self._read_json().get('topic', '')
            except Exception:
                self._send(400, json.dumps({'ok': False, 'error': 'inválido'}).encode(), 'application/json; charset=utf-8')
                return
            out = propose(topic, self._client_ip())
            self._send(200, json.dumps(out).encode(), 'application/json; charset=utf-8')
            return
        self._send(404, b'not found', 'text/plain')

    def do_GET(self):
        if self.path.startswith('/api/state'):
            self._send(200, json.dumps(state()).encode(), 'application/json; charset=utf-8')
        elif self.path.startswith('/api/blog'):
            self._send(200, json.dumps(blog_posts()).encode(), 'application/json; charset=utf-8')
        elif self.path == '/panel' or self.path.startswith('/panel?'):
            self._send(200, PANEL_PAGE.encode(), 'text/html; charset=utf-8')
        elif self.path == '/blog' or self.path.startswith('/blog?'):
            self._send(200, BLOG_PAGE.encode(), 'text/html; charset=utf-8')
        elif self.path.startswith('/blog/'):
            slug = self.path[len('/blog/'):].split('?')[0].strip('/')
            art = article_by_slug(slug) if slug else None
            if art:
                self._send(200, article_page(art).encode(), 'text/html; charset=utf-8')
            else:
                self._send(404, ('<!DOCTYPE html><meta charset="utf-8"><title>No encontrado</title>'
                                 '<body style="background:#070b12;color:#e7edf7;font-family:sans-serif;'
                                 'text-align:center;padding:80px 20px"><h1>404</h1>'
                                 '<p>Ese artículo no existe (todavía).</p>'
                                 '<a style="color:#5ac8fa" href="/blog">← Volver al blog</a></body>').encode(),
                           'text/html; charset=utf-8')
        elif self.path == '/rss.xml':
            self._send(200, rss_xml().encode(), 'application/rss+xml; charset=utf-8')
        elif self.path == '/favicon.svg':
            self._send(200, FAVICON.encode(), 'image/svg+xml')
        elif self.path == '/robots.txt':
            self._send(200, ROBOTS.encode(), 'text/plain; charset=utf-8')
        elif self.path == '/sitemap.xml':
            self._send(200, sitemap_xml().encode(), 'application/xml; charset=utf-8')
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
