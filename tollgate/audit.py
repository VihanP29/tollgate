"""SQLite audit log: every decision, the review queue, and replay against a new gate."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from tollgate.types import Action, DecisionRecord, DecisionResult, Gate

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    resolution TEXT
);
"""


class AuditLog:
    def __init__(self, path: str | Path = ":memory:"):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def record(self, action: Action, result: DecisionResult) -> DecisionRecord:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO decisions (created_at, action_json, result_json, verdict) VALUES (?, ?, ?, ?)",
                (now, action.model_dump_json(), result.model_dump_json(), result.verdict))
            self._db.commit()
        return DecisionRecord(decision_id=cur.lastrowid, created_at=now, action=action, result=result)

    def _rows(self, sql: str, args: tuple = ()) -> list[DecisionRecord]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [DecisionRecord(
            decision_id=r["decision_id"], created_at=r["created_at"],
            action=Action.model_validate_json(r["action_json"]),
            result=DecisionResult.model_validate_json(r["result_json"]),
            resolution=r["resolution"]) for r in rows]

    def list(self, limit: int = 50, since_id: int | None = None) -> list[DecisionRecord]:
        return self._rows("SELECT * FROM decisions WHERE decision_id > ? ORDER BY decision_id DESC LIMIT ?",
                          (since_id or 0, limit))

    def get(self, decision_id: int) -> DecisionRecord | None:
        rows = self._rows("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
        return rows[0] if rows else None

    def pending_review(self) -> list[DecisionRecord]:
        return self._rows("SELECT * FROM decisions WHERE verdict = 'escalate' AND resolution IS NULL "
                          "ORDER BY decision_id DESC")

    def resolve(self, decision_id: int, resolution: str) -> DecisionRecord | None:
        with self._lock:
            self._db.execute("UPDATE decisions SET resolution = ? WHERE decision_id = ?", (resolution, decision_id))
            self._db.commit()
        return self.get(decision_id)

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM decisions")
            self._db.commit()


def replay(log: AuditLog, gate: Gate, limit: int = 1000) -> list[dict]:
    """Re-run logged actions through another gate; return the ones whose verdict changed."""
    changed = []
    for rec in reversed(log.list(limit=limit)):
        new = gate.decide(rec.action)
        if new.verdict != rec.result.verdict:
            changed.append({"decision_id": rec.decision_id, "tool": rec.action.tool,
                            "old": rec.result.verdict, "new": new.verdict, "reason": new.reasons[0]})
    return changed


if __name__ == "__main__":
    from tollgate.engine import build_gate, demo_actions
    log, gate = AuditLog(), build_gate("heuristic")
    for a in demo_actions():
        rec = log.record(a, gate.decide(a))
        print(rec.decision_id, rec.result.verdict)
    print(json.dumps([r.decision_id for r in log.list()]))
