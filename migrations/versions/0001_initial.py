"""Initial private application schema.

Revision ID: 0001
"""
from alembic import op

from backitup.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS backitup")
    Base.metadata.create_all(op.get_bind())
    op.execute("INSERT INTO backitup.platform (id, used_bytes, reserved_bytes) VALUES (1, 0, 0)")
    op.execute("REVOKE ALL ON SCHEMA backitup FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA backitup FROM PUBLIC")


def downgrade():
    Base.metadata.drop_all(op.get_bind())
    op.execute("DROP SCHEMA backitup")
