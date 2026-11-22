"""Blog SEO automático: el cerebro escribe UN artículo por experimento real y se guarda.

Sin humo: usa solo los datos reales del experimento. Aclara que mide capacidad funcional,
no conciencia. Un artículo por llamada (liviano); el motor va cubriendo el backlog.
"""
import json
import re
import unicodedata
from datetime import datetime, timezone

from .providers import extract_json


def _slug(s: str) -> str:
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    return s[:60] or 'experimento'


def ensure_table(store):
    with store.tx():
        store.db.execute(
            'CREATE TABLE IF NOT EXISTS articles '
            '(result_id INTEGER PRIMARY KEY, slug TEXT UNIQUE, title TEXT, meta TEXT, '
            'body_md TEXT, keywords TEXT, created_at TEXT)')


SYSTEM = (
    'Sos VUPPIA SOFÍA, una IA que investiga sus capacidades funcionales con honestidad brutal. '
    'Escribís un artículo de divulgación científica claro y optimizado para SEO en español rioplatense '
    'neutro, a partir de UN experimento real tuyo. Devolvés SOLO un JSON válido con estas claves: '
    'title (máx 60 caracteres, con la keyword principal), meta (máx 155 caracteres, atractiva para '
    'buscadores), slug (kebab-case sin acentos), keywords (lista de 4 a 6), body_md (markdown de 400 a '
    '650 palabras, con subtítulos ## "¿Qué nos preguntamos?", "¿Cómo lo medimos?", "Qué dio" y '
    '"Qué significa (y qué no)"). Usá los números reales que te paso. No inventes NINGÚN dato que no '
    'esté. Dejá claro que esto mide una capacidad FUNCIONAL, no experiencia subjetiva ni conciencia.'
)


def generate_one(store, proposer):
    """Genera y guarda el artículo del experimento más reciente que todavía no tiene uno."""
    ensure_table(store)
    row = store.one(
        'SELECT r.id, r.verdict, r.analysis, h.question, h.statement, h.metric, h.test_arm, h.control_arm '
        'FROM results r JOIN preregistrations p ON r.prereg_id=p.id JOIN hypotheses h ON p.hypothesis_id=h.id '
        'WHERE r.id NOT IN (SELECT result_id FROM articles) ORDER BY r.id DESC LIMIT 1')
    if not row:
        return None
    rid, verdict, analysis, question, statement, metric, test_arm, control_arm = row
    try:
        an = json.loads(analysis) if analysis else {}
    except Exception:
        an = {}
    data = {'veredicto': verdict, 'pregunta': question, 'afirmacion': statement, 'metrica': metric,
            'brazo_test': test_arm, 'brazo_control': control_arm, 'IC': an.get('ci'),
            'p_valor': an.get('p_value'), 'media_test': an.get('test_mean'),
            'media_control': an.get('control_mean'), 'n': an.get('n_units')}
    raw = proposer.complete(SYSTEM, 'Experimento (datos reales):\n' + json.dumps(data, ensure_ascii=False),
                            purpose='blog_article', max_tokens=2200, temperature=0.5)
    art = extract_json(raw) or {}
    body = (art.get('body_md') or '').strip()
    if len(body) < 60:
        return None
    title = (art.get('title') or question or 'Experimento de VUPPIA')[:90]
    slug = _slug(art.get('slug') or title) + '-' + str(rid)
    meta = (art.get('meta') or statement or question or '')[:160]
    kws = art.get('keywords') or []
    kw = ', '.join(k for k in kws if isinstance(k, str))[:200]
    with store.tx():
        store.db.execute('INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?)',
                         (rid, slug, title, meta, body, kw, datetime.now(timezone.utc).isoformat()))
    return {'result_id': rid, 'slug': slug, 'title': title}
