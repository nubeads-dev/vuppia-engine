"""Persistencia: SQLite (WAL) con migraciones, bitácora encadenada y cola con leases."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

GENESIS = '0' * 64

MIGRATIONS = [
    # 1 — núcleo
    '''
    CREATE TABLE ledger (
        seq INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
        body TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')),
        lease_owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3, result TEXT, error TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, dedupe_key TEXT UNIQUE);
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL
            CHECK(kind IN ('question','hypothesis','finding','observation','method','note','anomaly')),
        content TEXT NOT NULL,
        provenance TEXT NOT NULL,      -- experiment:<id> | llm:<call_id> | operator | rule:<name>
        trust TEXT NOT NULL CHECK(trust IN ('measured','derived','unverified')),
        status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open','supported','refuted','inconclusive','retracted','trivial','invalid')),
        confidence REAL, data TEXT, created_at TEXT NOT NULL, supersedes INTEGER REFERENCES memories(id));
    CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='id');
    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END;
    CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END;
    CREATE TABLE memory_links (
        src INTEGER NOT NULL REFERENCES memories(id), dst INTEGER NOT NULL REFERENCES memories(id),
        relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts','follows_from','replicates','refines')),
        PRIMARY KEY (src, dst, relation));
    CREATE TABLE hypotheses (
        id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, origin TEXT NOT NULL,
        question TEXT NOT NULL, statement TEXT NOT NULL, protocol TEXT NOT NULL,
        params TEXT NOT NULL, test_arm TEXT NOT NULL, control_arm TEXT NOT NULL,
        metric TEXT NOT NULL, higher_is_better INTEGER NOT NULL, min_effect REAL NOT NULL,
        alpha REAL NOT NULL, n_units INTEGER NOT NULL, falsifier TEXT NOT NULL,
        alternatives TEXT NOT NULL, forecast REAL, forecast_by TEXT,
        priority REAL, parent_memory INTEGER, fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('proposed','rejected','preregistered','running','analyzed','failed')),
        review TEXT);
    CREATE TABLE preregistrations (
        id INTEGER PRIMARY KEY, hypothesis_id INTEGER NOT NULL UNIQUE REFERENCES hypotheses(id),
        sealed_at TEXT NOT NULL, record TEXT NOT NULL, record_sha TEXT NOT NULL, ledger_seq INTEGER NOT NULL);
    CREATE TABLE results (
        id INTEGER PRIMARY KEY, prereg_id INTEGER NOT NULL UNIQUE REFERENCES preregistrations(id),
        at TEXT NOT NULL, raw_sha TEXT NOT NULL, analysis TEXT NOT NULL, aa_analysis TEXT,
        verdict TEXT NOT NULL, forecast_brier REAL, runtime_s REAL, sandbox TEXT NOT NULL);
    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, purpose TEXT NOT NULL, reserved_usd REAL NOT NULL, cost_usd REAL,
        input_tokens INTEGER, output_tokens INTEGER, status TEXT NOT NULL, error TEXT);
    CREATE TABLE skill_versions (
        id INTEGER PRIMARY KEY, skill TEXT NOT NULL, sha TEXT NOT NULL, source TEXT NOT NULL,
        parent_sha TEXT, origin TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(skill, sha));
    CREATE TABLE skill_pointers (
        skill TEXT NOT NULL, channel TEXT NOT NULL CHECK(channel IN ('lab','production')),
        sha TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(skill, channel));
    CREATE TABLE skill_evals (
        id INTEGER PRIMARY KEY, skill TEXT NOT NULL, candidate_sha TEXT NOT NULL,
        incumbent_sha TEXT NOT NULL, at TEXT NOT NULL, eval_seed_commit TEXT NOT NULL,
        eval_seed TEXT, analysis TEXT NOT NULL, decision TEXT NOT NULL);
    ''',
    # 2 — reproducibilidad completa (corrección #3): datos crudos + entorno + huella de código
    '''
    ALTER TABLE results ADD COLUMN raw_json TEXT;
    ALTER TABLE results ADD COLUMN environment TEXT;
    ALTER TABLE results ADD COLUMN code_fp TEXT;
    ALTER TABLE skill_evals ADD COLUMN environment TEXT;
    ALTER TABLE skill_evals ADD COLUMN code_fp TEXT;
    CREATE TABLE approvals_used (
        token_id TEXT PRIMARY KEY, skill TEXT NOT NULL, candidate_sha TEXT NOT NULL,
        used_at TEXT NOT NULL, nonce TEXT NOT NULL, expires INTEGER NOT NULL);
    ''',
    # 3 — auditoría de llamadas a LLM (corrección #4): parámetros, respuesta y costo completos,
    # para auditar y reproducir aproximadamente cuando la salida no es determinista.
    '''
    ALTER TABLE llm_calls ADD COLUMN temperature REAL;
    ALTER TABLE llm_calls ADD COLUMN max_tokens INTEGER;
    ALTER TABLE llm_calls ADD COLUMN request_sha TEXT;
    ALTER TABLE llm_calls ADD COLUMN response_text TEXT;
    ALTER TABLE llm_calls ADD COLUMN hypothesis_id INTEGER;
    CREATE TABLE planner_decisions (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, policy TEXT NOT NULL, skill_sha TEXT NOT NULL,
        chosen TEXT NOT NULL, ranking TEXT NOT NULL, regret REAL, oracle_best TEXT, note TEXT);
    ''',
    # 4 — registro auditable de búsquedas externas sin elevar la evidencia a medida.
    '''
    CREATE TABLE research_runs (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, question TEXT NOT NULL,
        body TEXT NOT NULL, sha TEXT NOT NULL, status TEXT NOT NULL);
    CREATE INDEX research_runs_at ON research_runs(at);
    ''',
    # 5 — modo observación de la recuperación: registra divergencias FTS vs re-ranking
    # sin alterar lo que la búsqueda devuelve (corrección 3.5).
    '''
    CREATE TABLE retrieval_shadow (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, query TEXT NOT NULL,
        fts_order TEXT NOT NULL, rerank_order TEXT NOT NULL,
        top1_changed INTEGER NOT NULL, overlap_k REAL NOT NULL);
    ''',
    # 6 — plano de control conversacional: agenda de investigaciones y memoria de conversación,
    # COMPARTIDAS entre el chat y el motor autónomo (una sola continuidad operativa).
    '''
    CREATE TABLE agenda (
        id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        topic TEXT NOT NULL, question TEXT,
        status TEXT NOT NULL CHECK(status IN ('active','suspended','done','proposed')),
        priority REAL NOT NULL DEFAULT 1.0, origin TEXT NOT NULL, note TEXT);
    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user','sofia')),
        content TEXT NOT NULL, intent TEXT, refs TEXT);
    CREATE VIRTUAL TABLE conversations_fts USING fts5(content, content='conversations', content_rowid='id');
    CREATE TRIGGER conversations_ai AFTER INSERT ON conversations BEGIN
        INSERT INTO conversations_fts(rowid, content) VALUES (new.id, new.content); END;
    ''',
]


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def environment() -> dict:
    """Entorno exacto para reproducibilidad (corrección #3)."""
    import platform
    env = {'python': platform.python_version(), 'platform': platform.platform()}
    try:
        import numpy
        env['numpy'] = numpy.__version__
    except Exception:
        env['numpy'] = None
    try:
        from . import sandbox
        env['sandbox_image'] = sandbox.image_id()
        env['sandbox_image_tag'] = sandbox.DEFAULT_IMAGE
    except Exception:
        env['sandbox_image'] = None
    return env


def code_fingerprint() -> dict:
    """Hash de cada módulo del worker: identifica exactamente el código que corrió."""
    pkg = Path(__file__).resolve().parent
    out = {}
    for p in sorted(pkg.rglob('*.py')):
        if '__pycache__' in p.parts:
            continue
        out[str(p.relative_to(pkg))] = sha256(p.read_bytes())[:16]
    return out


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=30000')
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # --- transacciones explícitas (autocommit apagado manualmente) ---
    def tx(self, immediate: bool = False):
        t = _Tx(self.db); t.immediate = immediate
        return t

    def _migrate(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)')
        row = self.db.execute('SELECT v FROM schema_version').fetchone()
        current = row[0] if row else 0
        for i, script in enumerate(MIGRATIONS[current:], start=current + 1):
            with self.tx():
                for stmt in _split_sql(script):
                    self.db.execute(stmt)
                if current == 0 and i == 1:
                    self.db.execute('INSERT INTO schema_version VALUES (1)')
                else:
                    self.db.execute('UPDATE schema_version SET v=?', (i,))

    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    # --- bitácora a prueba de manipulación (encadenada por hash) ---
    def append(self, kind: str, body: dict) -> int:
        with self.tx():
            return self._append_in_tx(kind, body)

    def _append_in_tx(self, kind: str, body: dict) -> int:
        last = self.db.execute('SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1').fetchone()
        prev = last[0] if last else GENESIS
        at = utc()
        payload = canonical(body)
        digest = sha256(prev + '|' + at + '|' + kind + '|' + payload)
        cur = self.db.execute('INSERT INTO ledger(at,kind,body,prev_hash,hash) VALUES(?,?,?,?,?)',
                              (at, kind, payload, prev, digest))
        return cur.lastrowid

    def verify_ledger(self) -> dict:
        prev = GENESIS; n = 0
        for seq, at, kind, body, p, h in self.db.execute(
                'SELECT seq,at,kind,body,prev_hash,hash FROM ledger ORDER BY seq'):
            if p != prev or sha256(p + '|' + at + '|' + kind + '|' + body) != h:
                return {'ok': False, 'broken_at': seq, 'entries_checked': n}
            prev = h; n += 1
        return {'ok': True, 'entries': n, 'head': prev}

    # --- cola de trabajos con lease (recuperación de caídas) ---
    def enqueue(self, kind: str, payload: dict, dedupe_key: str | None = None, max_attempts=3) -> int | None:
        now = utc()
        with self.tx():
            try:
                cur = self.db.execute(
                    'INSERT INTO jobs(kind,payload,status,created_at,updated_at,dedupe_key,max_attempts) '
                    "VALUES(?,?,'queued',?,?,?,?)", (kind, canonical(payload), now, now, dedupe_key, max_attempts))
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def claim(self, owner: str, lease_s: float = 600) -> dict | None:
        now = time.time()
        with self.tx(immediate=True):
            row = self.db.execute(
                "SELECT id,kind,payload,attempts,max_attempts FROM jobs WHERE status='queued' "
                "OR (status='running' AND lease_until < ?) ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            jid, kind, payload, attempts, max_attempts = row
            if attempts >= max_attempts:
                self.db.execute("UPDATE jobs SET status='failed',error='max_attempts',updated_at=? WHERE id=?",
                                (utc(), jid))
                return None
            self.db.execute("UPDATE jobs SET status='running',lease_owner=?,lease_until=?,attempts=attempts+1,"
                            "updated_at=? WHERE id=?", (owner, now + lease_s, utc(), jid))
        return {'id': jid, 'kind': kind, 'payload': json.loads(payload), 'attempt': attempts + 1}

    def finish(self, jid: int, owner: str, result=None, error: str | None = None) -> bool:
        status = 'failed' if error else 'done'
        with self.tx():
            cur = self.db.execute('UPDATE jobs SET status=?,result=?,error=?,updated_at=?,lease_until=NULL '
                                  "WHERE id=? AND lease_owner=? AND status='running'",
                                  (status, canonical(result) if result is not None else None, error, utc(), jid, owner))
            return cur.rowcount == 1

    def requeue_failed_transient(self, jid: int):
        with self.tx():
            self.db.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=? AND attempts<max_attempts",
                            (utc(), jid))


class _Tx:
    def __init__(self, db):
        self.db = db; self.immediate = False

    def __enter__(self):
        self.db.execute('BEGIN IMMEDIATE' if self.immediate else 'BEGIN')
        return self.db

    def __exit__(self, exc_type, exc, tb):
        self.db.execute('ROLLBACK' if exc_type else 'COMMIT')
        return False


def _split_sql(script: str):
    """Divide el script respetando BEGIN...END de triggers."""
    out, buf, depth = [], [], 0
    for line in script.splitlines():
        s = line.strip()
        if not s:
            continue
        buf.append(line)
        up = s.upper()
        if up.startswith('CREATE TRIGGER'):
            depth = 1
        if depth and up.endswith('END;'):
            depth = 0
            out.append('\n'.join(buf)); buf = []
            continue
        if not depth and s.endswith(';'):
            out.append('\n'.join(buf)); buf = []
    if buf:
        out.append('\n'.join(buf))
    return out
