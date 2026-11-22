"""Agenda de investigaciones: estado de tareas COMPARTIDO entre el chat y el motor autónomo.

Una sola continuidad operativa: lo que vos pedís por el chat (suspender, priorizar, agregar)
y lo que el motor autónomo elige, leen y escriben la MISMA agenda. Diferencia estados:
- 'active'    : en curso, el motor la puede tomar.
- 'suspended' : frenada por vos; el motor NO la toma.
- 'proposed'  : propuesta (por SOFÍA o por vos), todavía sin aprobar para trabajar.
- 'done'      : terminada.
"""
from __future__ import annotations

from .store import Store, utc


def add(store: Store, topic: str, question: str = '', *, status='active', priority=1.0,
        origin='user', note='') -> int:
    topic = topic.strip()[:200]
    with store.tx() as db:
        cur = db.execute('INSERT INTO agenda(created_at,updated_at,topic,question,status,priority,origin,note) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (utc(), utc(), topic, question.strip()[:500], status, float(priority), origin, note[:300]))
        store._append_in_tx('agenda_add', {'id': cur.lastrowid, 'topic': topic, 'status': status})
    return cur.lastrowid


def set_status(store: Store, agenda_id: int, status: str) -> bool:
    if status not in ('active', 'suspended', 'done', 'proposed'):
        raise ValueError('estado inválido')
    with store.tx() as db:
        cur = db.execute('UPDATE agenda SET status=?,updated_at=? WHERE id=?', (status, utc(), agenda_id))
        if cur.rowcount:
            store._append_in_tx('agenda_status', {'id': agenda_id, 'status': status})
        return cur.rowcount == 1


def set_priority(store: Store, agenda_id: int, priority: float) -> bool:
    with store.tx() as db:
        cur = db.execute('UPDATE agenda SET priority=?,updated_at=? WHERE id=?',
                         (float(priority), utc(), agenda_id))
        if cur.rowcount:
            store._append_in_tx('agenda_priority', {'id': agenda_id, 'priority': priority})
        return cur.rowcount == 1


def top_active(store: Store) -> dict | None:
    row = store.one("SELECT id,topic,question,priority,origin FROM agenda WHERE status='active' "
                    'ORDER BY priority DESC, id ASC LIMIT 1')
    if not row:
        return None
    return dict(zip(['id', 'topic', 'question', 'priority', 'origin'], row))


def listing(store: Store, status: str | None = None, limit=50) -> list[dict]:
    sql = 'SELECT id,topic,question,status,priority,origin,updated_at FROM agenda'
    args = []
    if status:
        sql += ' WHERE status=?'; args.append(status)
    rows = store.q(sql + ' ORDER BY priority DESC, id DESC LIMIT ?', (*args, limit))
    cols = ['id', 'topic', 'question', 'status', 'priority', 'origin', 'updated_at']
    return [dict(zip(cols, r)) for r in rows]
