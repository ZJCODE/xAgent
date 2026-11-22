"""Durable reflection topics and per-part outbound acknowledgements."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path


def parse_decision(value):
    """Validate the reflection decision before any state or delivery changes."""
    if not isinstance(value, dict):
        raise ValueError('decision must be an object')
    action = value.get('action')
    fields = {
        'none': {'action'},
        'send': {'action', 'recipient', 'text'},
        'defer': {'action', 'recipient', 'subject', 'defer_reason', 'recheck_at', 'expires_at'},
        'close': {'action', 'followup_id', 'reason'},
    }
    if not isinstance(action, str) or action not in fields:
        raise ValueError('unknown decision action')
    required = fields[action]
    allowed = required | ({'followup_id'} if action in {'send', 'defer'} else set())
    if not required <= value.keys() or not value.keys() <= allowed:
        raise ValueError('invalid decision fields')
    if any(not isinstance(v, str) or not v.strip() for v in value.values()):
        raise ValueError('decision values must be nonempty strings')
    result = {k: v.strip() for k, v in value.items()}
    if action == 'defer':
        dates = [datetime.fromisoformat(result[k]) for k in ('recheck_at', 'expires_at')]
        if any(d.tzinfo is None or d.utcoffset() is None for d in dates):
            raise ValueError('follow-up times require timezone offsets')
        if not dates[0] < dates[1]:
            raise ValueError('follow-up requires recheck_at < expires_at')
    return result


class FollowupStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS followups (id TEXT PRIMARY KEY, state TEXT, revision INTEGER, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS deliveries (run_id TEXT, part_id TEXT, data TEXT, PRIMARY KEY(run_id, part_id))')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def due(self, now: float):
        if not self.path.exists():
            return []
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM followups WHERE state='pending'").fetchall()
            due = []
            for row in rows:
                data = json.loads(row['data'])
                if datetime.fromisoformat(data['expires_at']).timestamp() <= now:
                    db.execute("UPDATE followups SET state='closed', revision=revision+1 WHERE id=?", (row['id'],))
                elif datetime.fromisoformat(data['recheck_at']).timestamp() <= now:
                    due.append(dict(data, id=row['id'], revision=row['revision']))
            return sorted(due, key=lambda item: datetime.fromisoformat(item['recheck_at']).timestamp())[:10]

    def apply(self, decision, previous=None, *, sending=False):
        identifier = previous['id'] if previous else uuid.uuid4().hex
        state = 'sending' if sending else ('closed' if decision['action'] == 'close' else 'pending')
        with self.transaction() as db:
            if previous:
                changed = db.execute('UPDATE followups SET state=?, revision=revision+1, data=? WHERE id=? AND state=? AND revision=?',
                    (state, json.dumps(decision), identifier, 'pending', previous['revision'])).rowcount
                if not changed:
                    raise ValueError('follow-up changed since generation')
            else:
                # Require the existing id when the same open topic is proposed again.
                for row in db.execute("SELECT * FROM followups WHERE state='pending'").fetchall():
                    old = json.loads(row['data'])
                    if not sending and (old['recipient'], old['subject']) == (decision['recipient'], decision['subject']):
                        raise ValueError('use the existing followup_id to update this topic')
                db.execute('INSERT INTO followups VALUES (?, ?, 0, ?)', (identifier, state, json.dumps(decision)))
        return identifier

    def outcomes(self):
        if not self.path.exists():
            return []
        with self.transaction() as db:
            rows = db.execute("SELECT id, state, data FROM followups WHERE state IN ('accepted','sending','unknown') ORDER BY rowid DESC LIMIT 20").fetchall()
            return [dict(json.loads(row['data']), id=row['id'], delivery_state=row['state']) for row in rows]

    def release(self, identifier, previous):
        with self.transaction() as db:
            if previous:
                db.execute("UPDATE followups SET state='pending', revision=revision+1, data=? WHERE id=? AND state='sending'",
                           (json.dumps(previous), identifier))
            else:
                db.execute("UPDATE followups SET state='closed' WHERE id=? AND state='sending'", (identifier,))

    def finish(self, identifier, state):
        with self.transaction() as db:
            db.execute("UPDATE followups SET state=?, revision=revision+1 WHERE id=? AND state='sending'", (state, identifier))

    def postpone_checks(self, items, until):
        if not items:
            return
        with self.transaction() as db:
            for item in items:
                updated = dict(item, recheck_at=until)
                db.execute("UPDATE followups SET data=?, revision=revision+1 WHERE id=? AND state='pending' AND revision=?",
                    (json.dumps(updated), item['id'], item['revision']))

    # This interface is shared with the scheduled-task delivery journal.
    def delivery(self, run_id, part_id):
        with self.transaction() as db:
            row = db.execute('SELECT data FROM deliveries WHERE run_id=? AND part_id=?', (run_id, part_id)).fetchone()
            return json.loads(row[0]) if row else None

    def has_unconfirmed_delivery(self, run_id):
        with self.transaction() as db:
            rows = db.execute('SELECT data FROM deliveries WHERE run_id=?', (run_id,)).fetchall()
            return any(json.loads(row[0])['status'] in {'sending', 'needs_review'} for row in rows)

    def save_delivery(self, run_id, part):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?)', (run_id, part['id'], json.dumps(part)))
