"""Proveedores de modelos con presupuesto en USD.

Cada llamada reserva el costo del PEOR caso (tokens de entrada estimados +
max_tokens de salida) ANTES de llamar, bajo transacción IMMEDIATE; después se
concilia con el uso real informado por la API. Si el día no tiene saldo, no se
llama. Esto es un tope local: configurar también límites en la consola del
proveedor.

Las respuestas de los modelos son DATOS no confiables: nunca se ejecutan como
instrucciones del sistema y se validan contra esquemas estrictos.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .store import Store, canonical, sha256, utc

# USD por millón de tokens (entrada, salida). Fuente: páginas oficiales de precios,
# verificadas el 16/11/2026. Sobrescribible con VUPPIA_PRICES='{"modelo":[in,out]}'.
PRICES = {
    'claude-haiku-5-5': (0.10, 0.50),
    'claude-sonnet-5-5': (2.00, 10.00),
    'claude-opus-5-5': (4.00, 20.00),
    'gpt-6-luna': (0.10, 0.50),
    'gpt-6.1-sol': (2.00, 10.00),
    'fake': (0.0, 0.0), 'fake-a': (0.0, 0.0), 'fake-b': (0.0, 0.0),
    # --- Modelos de tier GRATIS (costo 0). Si usas otro, agregalo con VUPPIA_PRICES. ---
    'llama-3.3-70b-versatile': (0.0, 0.0), 'llama-3.1-8b-instant': (0.0, 0.0),
    'openai/gpt-oss-120b': (0.0, 0.0), 'openai/gpt-oss-20b': (0.0, 0.0),
    'qwen/qwen3-32b': (0.0, 0.0), 'moonshotai/kimi-k2-instruct': (0.0, 0.0),
    'gemini-2.0-flash': (0.0, 0.0), 'gemini-2.5-flash': (0.0, 0.0),
}
MAX_RESPONSE_BYTES = 200_000


class ProviderError(RuntimeError):
    pass


class BudgetExceeded(ProviderError):
    pass


def price(model: str) -> tuple[float, float]:
    table = dict(PRICES)
    extra = os.getenv('VUPPIA_PRICES')
    if extra:
        table.update({k: tuple(v) for k, v in json.loads(extra).items()})
    if model not in table:
        raise ProviderError(f'Modelo sin precio conocido: {model}. Definilo en VUPPIA_PRICES.')
    return table[model]


def daily_budget() -> float:
    return float(os.getenv('VUPPIA_DAILY_BUDGET_USD', '1.00'))


def monthly_budget() -> float:
    return float(os.getenv('VUPPIA_MONTHLY_BUDGET_USD', '10.00'))


def per_call_cap() -> float:
    return float(os.getenv('VUPPIA_PER_CALL_CAP_USD', '0.25'))


class Provider:
    name = 'base'

    def __init__(self, store: Store, model: str):
        self.store, self.model = store, model

    def complete(self, system: str, user: str, *, purpose: str, max_tokens: int = 800,
                 temperature: float | None = None, hypothesis_id: int | None = None) -> str:
        pin, pout = price(self.model)
        est_in = (len(system) + len(user)) / 3.0         # estimación conservadora de tokens
        reserve = (est_in * pin + max_tokens * pout) / 1e6
        req_sha = sha256(canonical({'model': self.model, 'system': system, 'user': user,
                                    'max_tokens': max_tokens, 'temperature': temperature}))
        call_id = self._reserve(reserve, purpose)
        self._record_request(call_id, temperature, max_tokens, req_sha, hypothesis_id)
        try:
            text, tin, tout = self._call(system, user, max_tokens, temperature)
        except Exception as exc:
            self._settle(call_id, None, None, None, 'error', type(exc).__name__ + ': ' + str(exc)[:200], None)
            self._persist_quota()
            raise
        cost = (tin * pin + tout * pout) / 1e6 if tin is not None else reserve
        # Guardar la respuesta completa (recortada) para auditoría/reproducción aproximada.
        self._settle(call_id, cost, tin, tout, 'ok', None, text[:8000])
        self._persist_quota()
        self.last_call_id = call_id
        return text

    def _persist_quota(self):
        """Guarda la última cuota vista (headers x-ratelimit-*) en kv para el panel."""
        if not _LAST_RL:
            return
        try:
            snap = dict(_LAST_RL)
            snap['provider'] = self.name
            snap['model'] = self.model
            with self.store.tx() as db:
                db.execute('CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)')
                db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('provider_quota',?)",
                           (json.dumps(snap),))
        except Exception:                               # noqa: BLE001
            pass

    def _record_request(self, call_id, temperature, max_tokens, req_sha, hypothesis_id):
        with self.store.tx() as db:
            db.execute('UPDATE llm_calls SET temperature=?,max_tokens=?,request_sha=?,hypothesis_id=? WHERE id=?',
                       (temperature, max_tokens, req_sha, hypothesis_id, call_id))

    def _reserve(self, usd: float, purpose: str) -> int:
        day = utc()[:10]
        month = utc()[:7]
        if usd > per_call_cap():
            raise BudgetExceeded(f'La llamada reserva {usd:.4f} USD > tope por llamada {per_call_cap():.2f} USD')
        with self.store.tx(immediate=True) as db:
            spent_day = db.execute('SELECT COALESCE(SUM(COALESCE(cost_usd, reserved_usd)),0) FROM llm_calls '
                                   'WHERE day=?', (day,)).fetchone()[0]
            spent_month = db.execute('SELECT COALESCE(SUM(COALESCE(cost_usd, reserved_usd)),0) FROM llm_calls '
                                     'WHERE substr(day,1,7)=?', (month,)).fetchone()[0]
            if spent_day + usd > daily_budget():
                raise BudgetExceeded(f'Presupuesto diario agotado: {spent_day:.4f} + {usd:.4f} '
                                     f'> {daily_budget():.2f} USD')
            if spent_month + usd > monthly_budget():
                raise BudgetExceeded(f'Presupuesto mensual agotado: {spent_month:.4f} + {usd:.4f} '
                                     f'> {monthly_budget():.2f} USD')
            cur = db.execute('INSERT INTO llm_calls(at,day,provider,model,purpose,reserved_usd,status) '
                             "VALUES(?,?,?,?,?,?,'reserved')", (utc(), day, self.name, self.model, purpose, usd))
            return cur.lastrowid

    def _settle(self, call_id, cost, tin, tout, status, error, response_text):
        # Un error de red/HTTP puede haber sido facturado: se conserva la reserva como costo.
        with self.store.tx() as db:
            db.execute('UPDATE llm_calls SET cost_usd=COALESCE(?, reserved_usd),input_tokens=?,output_tokens=?,'
                       'status=?,error=?,response_text=? WHERE id=?',
                       (cost, tin, tout, status, error, response_text, call_id))

    def _call(self, system, user, max_tokens, temperature):  # pragma: no cover - interfaz
        raise NotImplementedError


# Última cuota vista en los headers del proveedor (por proceso). Groq/OpenAI-compat
# devuelven x-ratelimit-* en cada respuesta; los guardamos para mostrarlos en el panel.
_LAST_RL: dict = {}


def _capture_rl(hdrs):
    try:
        rl = {}
        for k in ('limit-requests', 'remaining-requests', 'reset-requests',
                  'limit-tokens', 'remaining-tokens', 'reset-tokens'):
            v = hdrs.get('x-ratelimit-' + k)
            if v is not None:
                rl[k.replace('-', '_')] = v
        ra = hdrs.get('retry-after')
        if ra is not None:
            rl['retry_after'] = ra
        if rl:
            rl['at'] = utc()
            _LAST_RL.clear()
            _LAST_RL.update(rl)
    except Exception:                                   # noqa: BLE001
        pass


def _retry_after_seconds(hdrs, default):
    try:
        ra = hdrs.get('retry-after')
        if ra is None:
            return default
        return min(float(ra), 30.0)
    except Exception:                                   # noqa: BLE001
        return default


def _post(url: str, payload: dict, headers: dict, timeout=90) -> dict:
    req = urllib.request.Request(url, json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'vuppia-sofia/1.0 (+https://vuppia.com)', **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _capture_rl(r.headers)
                raw = r.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderError('respuesta demasiado grande')
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                _capture_rl(e.headers)
            except Exception:                           # noqa: BLE001
                pass
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                wait = _retry_after_seconds(getattr(e, 'headers', None) or {}, 2 ** attempt * 3) \
                    if e.code == 429 else 2 ** attempt * 3
                time.sleep(wait); continue
            raise ProviderError(f'HTTP {e.code}') from e
        except urllib.error.URLError as e:
            raise ProviderError('sin conexión con el proveedor') from e
    raise ProviderError('reintentos agotados')


class Anthropic(Provider):
    name = 'anthropic'

    def _call(self, system, user, max_tokens, temperature):
        key = os.getenv('ANTHROPIC_API_KEY')
        if not key:
            raise ProviderError('Falta ANTHROPIC_API_KEY')
        body = {'model': self.model, 'max_tokens': max_tokens, 'system': system,
                'messages': [{'role': 'user', 'content': user}]}
        if temperature is not None:
            body['temperature'] = temperature
        data = _post('https://api.anthropic.com/v1/messages', body,
                     {'x-api-key': key, 'anthropic-version': '2023-06-01'})
        text = ''.join(p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text')
        u = data.get('usage', {})
        return text, u.get('input_tokens'), u.get('output_tokens')


class OpenAI(Provider):
    name = 'openai'

    def _call(self, system, user, max_tokens, temperature):
        key = os.getenv('OPENAI_API_KEY')
        if not key:
            raise ProviderError('Falta OPENAI_API_KEY')
        body = {'model': self.model, 'max_completion_tokens': max_tokens,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if temperature is not None:
            body['temperature'] = temperature
        data = _post('https://api.openai.com/v1/chat/completions', body, {'Authorization': 'Bearer ' + key})
        text = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        u = data.get('usage', {})
        return text, u.get('prompt_tokens'), u.get('completion_tokens')


class Fake(Provider):
    """Proveedor determinista para TESTS del pipeline. No es investigación."""
    name = 'fake'

    def __init__(self, store, model='fake', responder=None):
        super().__init__(store, model)       # respeta el nombre de modelo (para independencia)
        self.responder = responder or (lambda s, u: '{}')

    def _call(self, system, user, max_tokens, temperature):
        return self.responder(system, user), len(user) // 4, 50


class _OAICompat(Provider):
    """Base para proveedores compatibles con el formato OpenAI (chat/completions).
    Usada por los tiers GRATIS. La key se lee de una variable de entorno del servidor,
    NUNCA del chat ni del codigo."""
    API_URL = ''
    KEY_ENV = ''
    EXTRA_HEADERS: dict = {}

    def _call(self, system, user, max_tokens, temperature):
        key = os.getenv(self.KEY_ENV)
        if not key:
            raise ProviderError(f'Falta {self.KEY_ENV}')
        body = {'model': self.model, 'max_tokens': max_tokens,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if temperature is not None:
            body['temperature'] = temperature
        if 'gpt-oss' in self.model:
            body['reasoning_effort'] = 'low'  # libera tokens para la respuesta y acelera
        headers = {'Authorization': 'Bearer ' + key}
        headers.update(self.EXTRA_HEADERS)
        data = _post(self.API_URL, body, headers)
        text = (data.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        u = data.get('usage', {})
        return text, u.get('prompt_tokens'), u.get('completion_tokens')


class Groq(_OAICompat):
    """GRATIS. https://console.groq.com para la key (GROQ_API_KEY)."""
    name = 'groq'
    API_URL = 'https://api.groq.com/openai/v1/chat/completions'
    KEY_ENV = 'GROQ_API_KEY'


class OpenRouter(_OAICompat):
    """Tiene modelos GRATIS. https://openrouter.ai para la key (OPENROUTER_API_KEY)."""
    name = 'openrouter'
    API_URL = 'https://openrouter.ai/api/v1/chat/completions'
    KEY_ENV = 'OPENROUTER_API_KEY'


class Gemini(_OAICompat):
    """GRATIS (tier free). https://aistudio.google.com para la key (GEMINI_API_KEY)."""
    name = 'gemini'
    API_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    KEY_ENV = 'GEMINI_API_KEY'


def from_env(store: Store, role: str) -> Provider | None:
    """role: proposer | reviewer | subject. Ej.: VUPPIA_PROPOSER=anthropic:claude-sonnet-5-5"""
    spec = os.getenv('VUPPIA_' + role.upper())
    if not spec:
        return None
    prov, _, model = spec.partition(':')
    cls = {'anthropic': Anthropic, 'openai': OpenAI,
           'groq': Groq, 'openrouter': OpenRouter, 'gemini': Gemini}.get(prov)
    if not cls or not model:
        raise ProviderError(f'VUPPIA_{role.upper()} inválido: usar proveedor:modelo')
    return cls(store, model)


def extract_json(text: str) -> dict:
    """Extrae el primer objeto JSON de una respuesta (tolera ```json)."""
    t = text.strip()
    if t.startswith('```'):
        t = t.split('\n', 1)[1] if '\n' in t else ''
        t = t.rsplit('```', 1)[0]
    start = t.find('{')
    if start < 0:
        raise ProviderError('la respuesta no contiene JSON')
    depth = 0; in_str = False; esc = False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"': in_str = True
        elif ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ProviderError('JSON incompleto')
