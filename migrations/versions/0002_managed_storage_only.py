"""Remove customer-managed storage configuration.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # Do not silently make existing external backups unrecoverable. An operator
    # with legacy customer-managed snapshots must migrate them into BackItUp
    # Cloud before running this schema change.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'backitup'
              AND table_name = 'storage_profiles'
              AND column_name = 'kind'
          ) THEN
            IF EXISTS (
              SELECT 1 FROM backitup.storage_profiles WHERE kind <> 'managed_r2'
            ) THEN
              RAISE EXCEPTION
                'Cannot remove customer-managed storage while legacy profiles remain; migrate snapshots to BackItUp Cloud first.';
            END IF;
            IF (SELECT count(*) FROM backitup.storage_profiles GROUP BY workspace_id HAVING count(*) > 1 LIMIT 1) IS NOT NULL THEN
              RAISE EXCEPTION
                'Cannot remove customer-managed storage while a workspace has multiple legacy profiles; migrate snapshots to one BackItUp Cloud profile first.';
            END IF;
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE backitup.storage_profiles DROP COLUMN IF EXISTS encrypted_credentials")
    op.execute("ALTER TABLE backitup.storage_profiles DROP COLUMN IF EXISTS verified_at")
    op.execute("ALTER TABLE backitup.storage_profiles DROP COLUMN IF EXISTS prune_authorized")
    op.execute("ALTER TABLE backitup.storage_profiles DROP COLUMN IF EXISTS region")
    op.execute("ALTER TABLE backitup.storage_profiles DROP COLUMN IF EXISTS kind")
    op.execute(
        """
        DO $$
        BEGIN
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
    op.execute("ALTER TABLE backitup.storage_profiles ADD COLUMN IF NOT EXISTS kind varchar(20) NOT NULL DEFAULT 'managed_r2'")
    op.execute("ALTER TABLE backitup.storage_profiles ADD COLUMN IF NOT EXISTS region varchar(50) NOT NULL DEFAULT 'auto'")
    op.execute("ALTER TABLE backitup.storage_profiles ADD COLUMN IF NOT EXISTS encrypted_credentials text")
    op.execute("ALTER TABLE backitup.storage_profiles ADD COLUMN IF NOT EXISTS verified_at timestamp with time zone")
    op.execute("ALTER TABLE backitup.storage_profiles ADD COLUMN IF NOT EXISTS prune_authorized boolean NOT NULL DEFAULT true")
