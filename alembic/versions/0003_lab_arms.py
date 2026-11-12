"""Keep comparison arm identity plus immutable policy ID in typed storage."""
from alembic import op
import sqlalchemy as sa

revision = '0003_lab_arms'
down_revision = '0002_runs'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('experiment_rows') as batch:
        batch.alter_column('arm', existing_type=sa.String(30), type_=sa.String(120), existing_nullable=False)


def downgrade():
    # Existing longer identities must not be truncated by a downgrade.
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT COUNT(*) FROM experiment_rows WHERE LENGTH(arm) > 30')).scalar():
        raise RuntimeError('Long comparison identities exist; restore a compatible backup instead of truncating history')
    with op.batch_alter_table('experiment_rows') as batch:
        batch.alter_column('arm', existing_type=sa.String(120), type_=sa.String(30), existing_nullable=False)
