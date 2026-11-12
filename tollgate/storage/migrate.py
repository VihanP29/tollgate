"""Run the pinned schema migration through Alembic, without copying legacy data."""
from pathlib import Path
from alembic import command
from alembic.config import Config


def upgrade(url):
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # Pass the URL as an attribute so an unrelated env override cannot change the target.
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
