"""Short SQLAlchemy units of work; SQLite is explicitly a local compatibility backend."""
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.pool import StaticPool
from tollgate.storage import schema as s


class Database:
    def __init__(self, url):
        sqlite = url.startswith("sqlite")
        if sqlite and ":memory:" not in url:
            path = url.split("///", 1)[1]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        options = {"pool_pre_ping": True}
        if sqlite:
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.sqlite = sqlite
        if sqlite:
            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA busy_timeout=30000")
            @event.listens_for(self.engine, "begin")
            def begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def transaction(self, workspace_id=None):
        with self.engine.begin() as connection:
            if workspace_id:
                row = connection.execute(select(s.workspaces).where(s.workspaces.c.id == workspace_id).with_for_update()).mappings().first()
                if row is None:
                    raise LookupError("Resource unavailable")
            yield connection

    def ready(self):
        with self.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            return revision == "0004_model_usage"

    def dispose(self):
        self.engine.dispose()


def row(connection, table, wid, resource_id, required=True):
    result = connection.execute(select(table).where(table.c.workspace_id == wid, table.c.id == resource_id)).mappings().first()
    if result is None and required:
        raise LookupError("Resource unavailable")
    return dict(result) if result is not None else None


def change(connection, table, wid, resource_id, **values):
    return connection.execute(table.update().where(table.c.workspace_id == wid, table.c.id == resource_id).values(**values))
