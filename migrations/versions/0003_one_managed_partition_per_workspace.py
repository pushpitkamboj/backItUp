"""Give every workspace exactly one internal storage partition.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM backitup.storage_profiles
            GROUP BY workspace_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'A workspace has multiple storage profiles; migrate its snapshots to one BackItUp Cloud partition before upgrading.';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'storage_profiles_workspace_id_key'
              AND conrelid = 'backitup.storage_profiles'::regclass
          ) THEN
            ALTER TABLE backitup.storage_profiles
              ADD CONSTRAINT storage_profiles_workspace_id_key UNIQUE (workspace_id);
          END IF;
        END $$;
        """
    )


def downgrade():
    op.execute("ALTER TABLE backitup.storage_profiles DROP CONSTRAINT IF EXISTS storage_profiles_workspace_id_key")
