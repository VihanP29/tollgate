import os
from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from tollgate.storage.schema import metadata

from pathlib import Path
from tollgate.settings import load_env
load_env(Path(__file__).resolve().parents[1] / ".env", names={"DATABASE_URL"})

config = context.config
url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
if url:
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    target = make_url(config.get_main_option("sqlalchemy.url"))
    if target.get_backend_name() == "sqlite" and target.database and target.database != ":memory:":
        Path(target.database).parent.mkdir(parents=True, exist_ok=True)
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
