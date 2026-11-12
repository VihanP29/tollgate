"""Explicit, transactional read-only legacy archive import. No operational replay is inferred."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from sqlalchemy import select
from tollgate.authorization import ControlSession
from tollgate.decision_context import identity
from tollgate.services.platform import new_id
from tollgate.storage import schema as s
from tollgate.storage.database import Database
from tollgate.settings import Settings


def import_legacy(database, source_path, workspace_id, apply=False):
    source = Path(source_path).resolve(strict=True)
    if database.sqlite and database.engine.url.database and Path(database.engine.url.database).resolve() == source:
        raise ValueError("Legacy source and target must differ")
    connection = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)
    try:
        records = connection.execute("SELECT id, state FROM control_sessions ORDER BY id").fetchall()
        history = connection.execute("SELECT id, session_id, at, kind, payload FROM control_history ORDER BY id").fetchall()
    finally:
        connection.close()
    validated = []
    for sid, raw in records:
        session = ControlSession.model_validate_json(raw)
        if session.session_id != sid:
            raise ValueError("Legacy session identity mismatch")
        spent = sum(payment["amount_cents"] for payment in session.ledger)
        if any(type(payment["amount_cents"]) is not int or payment["amount_cents"] <= 0 for payment in session.ledger):
            raise ValueError("Invalid legacy receipt amounts")
        if spent > session.mandate.budget_cents:
            raise ValueError("Legacy receipt/budget reconciliation failed")
        validated.append((sid, raw, hashlib.sha256(raw.encode()).hexdigest(), len(session.ledger), spent))
    report = {"schema_version": 1, "source_path": str(source), "workspace_id": workspace_id,
              "dry_run": not apply, "sessions": len(records), "receipts": sum(v[3] for v in validated),
              "spent_minor": sum(v[4] for v in validated), "new_archives": 0, "already_imported": 0,
              "replay_scope": "Read-only legacy archives; canonical financial replay/operational resumption not available"}
    with database.transaction(workspace_id) as c:
        for sid, raw, source_hash, count, spent in validated:
            old = c.execute(select(s.legacy.c.id).where(s.legacy.c.workspace_id == workspace_id,
                s.legacy.c.source_id == sid, s.legacy.c.source_hash == source_hash)).first()
            if old:
                report["already_imported"] += 1
                continue
            report["new_archives"] += 1
            if apply:
                payload = {"source_raw_json": raw, "source_hash": source_hash, "replay_scope": "legacy_read_only",
                           "history": [dict(sequence=h[0], at=h[2], kind=h[3], payload=json.loads(h[4])) for h in history if h[1] == sid]}
                c.execute(s.legacy.insert().values(workspace_id=workspace_id, id=new_id("archive"), source_id=sid,
                    source_hash=source_hash, payload=payload, receipt_count=count, spent_minor=spent))
        if apply:
            from tollgate.services.platform import Platform
            Platform(database, Settings()).audit(c, workspace_id, "cli:import", "legacy_import_completed", workspace_id,
                                                 {k: v for k, v in report.items() if k != "source_path"})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_db")
    parser.add_argument("workspace_id")
    parser.add_argument("--apply", action="store_true", help="Without this flag, produce a dry-run report only")
    args = parser.parse_args()
    db = Database(Settings.from_env().database_url)
    try:
        print(json.dumps(import_legacy(db, args.source_db, args.workspace_id, args.apply), indent=2))
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
