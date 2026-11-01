"""The memory ssisbliss does not need and a monitor does, in one SQLite file on the machine that watches: alerts from
open to resolved, what each job looked like at the last pass, and when each environment was last looked at."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .activity import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, env TEXT NOT NULL, rule TEXT NOT NULL, subject TEXT NOT NULL, severity TEXT NOT NULL,
    message TEXT NOT NULL, next TEXT, opened_at TEXT NOT NULL, last_seen TEXT NOT NULL, passes INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT, acked_at TEXT, ack_note TEXT, told_at TEXT, told_resolved_at TEXT, facts TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_open ON alerts (env, rule, subject) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS jobs (env TEXT NOT NULL, job TEXT NOT NULL, memory TEXT NOT NULL, PRIMARY KEY (env, job));
CREATE TABLE IF NOT EXISTS passes (env TEXT PRIMARY KEY, at TEXT NOT NULL, server TEXT, unread TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "facts" not in [c[1] for c in self.db.execute("PRAGMA table_info(alerts)")]:     # a database from before facts
            self.db.execute("ALTER TABLE alerts ADD COLUMN facts TEXT")

    def memory(self, env: str) -> dict[str, dict]:
        return {r["job"]: json.loads(r["memory"]) for r in self.db.execute("SELECT job, memory FROM jobs WHERE env = ?", (env,))}

    def remember(self, env: str, memory: dict[str, dict], at: datetime, server: str | None, unread: dict) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)",
                                [(env, job, json.dumps(m)) for job, m in memory.items()])
            self.db.execute("INSERT OR REPLACE INTO passes VALUES (?, ?, ?, ?)",
                            (env, at.isoformat()[:19], server, json.dumps(unread)))

    def apply(self, env: str, signals: list[Signal], looked: set[str], at: datetime) -> None:
        """Open what is new, refresh what stays, resolve what is gone: only for rules that really looked."""
        now = at.isoformat()[:19]
        with self.db:
            current = {(s.rule, s.subject) for s in signals}
            for s in signals:
                facts = json.dumps({"at": s.at, "in_a_row": s.in_a_row, "blocks": s.blocks, "person": s.person, "caused_by": s.caused_by})
                self.db.execute("""INSERT INTO alerts (env, rule, subject, severity, message, next, opened_at, last_seen, facts)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT (env, rule, subject) WHERE resolved_at IS NULL DO UPDATE SET
                                       severity = excluded.severity, message = excluded.message, next = excluded.next,
                                       last_seen = excluded.last_seen, passes = passes + 1, facts = excluded.facts""",
                                (s.env, s.rule, s.subject, s.severity, s.message, s.next, now, now, facts))
            for r in self.db.execute("SELECT id, rule, subject FROM alerts WHERE env = ? AND resolved_at IS NULL", (env,)).fetchall():
                if r["rule"] in looked and (r["rule"], r["subject"]) not in current:
                    self.db.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (now, r["id"]))

    def to_tell(self, at: datetime, remind_hours: float) -> tuple[list[dict], list[dict]]:
        """(alerts to send: new, or unacknowledged and last told long enough ago; resolved ones nobody was told of)."""
        before = (at - timedelta(hours=remind_hours)).isoformat()[:19]
        opened = self.db.execute("""SELECT * FROM alerts WHERE resolved_at IS NULL AND acked_at IS NULL
                                    AND (told_at IS NULL OR told_at <= ?) ORDER BY id""", (before,)).fetchall()
        closed = self.db.execute("""SELECT * FROM alerts WHERE resolved_at IS NOT NULL AND told_at IS NOT NULL
                                    AND told_resolved_at IS NULL ORDER BY id""").fetchall()
        return [dict(r) for r in opened], [dict(r) for r in closed]

    def told(self, opened: list[dict], closed: list[dict], at: datetime) -> None:
        now = at.isoformat()[:19]
        with self.db:
            self.db.executemany("UPDATE alerts SET told_at = ? WHERE id = ?", [(now, a["id"]) for a in opened])
            self.db.executemany("UPDATE alerts SET told_resolved_at = ? WHERE id = ?", [(now, a["id"]) for a in closed])

    def alerts(self, everything: bool = False) -> list[dict]:
        where = "" if everything else "WHERE resolved_at IS NULL"
        order = "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, opened_at"
        rows = [dict(r) for r in self.db.execute(f"SELECT * FROM alerts {where} ORDER BY resolved_at IS NOT NULL, {order}")]
        return [{**r, **json.loads(r.get("facts") or "{}")} for r in rows]

    def ack(self, alert: int, note: str | None, at: datetime) -> bool:
        with self.db:
            return self.db.execute("UPDATE alerts SET acked_at = ?, ack_note = ? WHERE id = ? AND resolved_at IS NULL",
                                   (at.isoformat()[:19], note, alert)).rowcount == 1

    def passes(self) -> list[dict]:
        return [{**dict(r), "unread": json.loads(r["unread"])} for r in self.db.execute("SELECT * FROM passes ORDER BY env")]
