import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "backitup"


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class IDMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Workspace(IDMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = {"schema": SCHEMA}
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    recovery_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class Platform(Base):
    __tablename__ = "platform"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class TenantMixin(IDMixin):
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)


class Connection(TenantMixin, Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("workspace_id", "purpose"), UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id"], [f"{SCHEMA}.workspaces.id"]), {"schema": SCHEMA})
    purpose: Mapped[str] = mapped_column(String(20))
    provider_user_id: Mapped[int] = mapped_column(BigInteger)
    login: Mapped[str] = mapped_column(String(100))
    encrypted_token: Mapped[str] = mapped_column(Text)


class Installation(TenantMixin, Base):
    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("workspace_id", "provider_id"), UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id"], [f"{SCHEMA}.workspaces.id"]), {"schema": SCHEMA})
    provider_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str] = mapped_column(String(100))
    permissions: Mapped[dict] = mapped_column(JSONB, default=dict)


class StorageProfile(TenantMixin, Base):
    __tablename__ = "storage_profiles"
    __table_args__ = (UniqueConstraint("workspace_id"), UniqueConstraint("workspace_id", "id"), ForeignKeyConstraint(["workspace_id"], [f"{SCHEMA}.workspaces.id"]), {"schema": SCHEMA})
    name: Mapped[str] = mapped_column(String(100))
    bucket: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(512))
    account_id: Mapped[str] = mapped_column(String(32))


class Repository(TenantMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("workspace_id", "provider_id"), UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id", "installation_id"], [f"{SCHEMA}.installations.workspace_id", f"{SCHEMA}.installations.id"]), {"schema": SCHEMA})
    provider_id: Mapped[int] = mapped_column(BigInteger)
    installation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    private: Mapped[bool] = mapped_column(Boolean, default=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")


class Plan(TenantMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("repository_id"), UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id", "repository_id"], [f"{SCHEMA}.repositories.workspace_id", f"{SCHEMA}.repositories.id"]),
                      ForeignKeyConstraint(["workspace_id", "storage_id"], [f"{SCHEMA}.storage_profiles.workspace_id", f"{SCHEMA}.storage_profiles.id"]),
                      CheckConstraint("interval_hours IN (0, 6, 24)"), CheckConstraint("retention_days >= 1 AND retention_days <= 3650"),
                      Index("ix_plans_due", "enabled", "next_due"), {"schema": SCHEMA})
    repository_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    storage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    categories: Mapped[list] = mapped_column(JSONB, default=list)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    retention_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Worker(IDMixin, Base):
    __tablename__ = "workers"
    __table_args__ = {"schema": SCHEMA}
    name: Mapped[str] = mapped_column(String(100), unique=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Job(TenantMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("workspace_id", "id"), UniqueConstraint("workspace_id", "idempotency_key"),
                      ForeignKeyConstraint(["workspace_id", "repository_id"], [f"{SCHEMA}.repositories.workspace_id", f"{SCHEMA}.repositories.id"]),
                      ForeignKeyConstraint(["workspace_id", "plan_id"], [f"{SCHEMA}.plans.workspace_id", f"{SCHEMA}.plans.id"]),
                      ForeignKeyConstraint(["worker_id"], [f"{SCHEMA}.workers.id"]),
                      CheckConstraint("state IN ('queued','leased','running','committing','succeeded','retry_wait','failed','cancelled')"),
                      Index("ix_jobs_lease_queue", "state", "available_at", "priority"),
                      Index("ix_jobs_active_repository", "repository_id", unique=True, postgresql_where="state IN ('leased','running','committing')"), {"schema": SCHEMA})
    repository_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(30), default="backup")
    state: Mapped[str] = mapped_column(String(20), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=10)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_version: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class Snapshot(TenantMixin, Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("job_id"), UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id", "job_id"], [f"{SCHEMA}.jobs.workspace_id", f"{SCHEMA}.jobs.id"]),
                      ForeignKeyConstraint(["workspace_id", "repository_id"], [f"{SCHEMA}.repositories.workspace_id", f"{SCHEMA}.repositories.id"]),
                      ForeignKeyConstraint(["workspace_id", "storage_id"], [f"{SCHEMA}.storage_profiles.workspace_id", f"{SCHEMA}.storage_profiles.id"]), {"schema": SCHEMA})
    repository_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    storage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    object_prefix: Mapped[str] = mapped_column(String(1024))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    coverage: Mapped[dict] = mapped_column(JSONB)
    manifest_summary: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="complete")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Reservation(TenantMixin, Base):
    __tablename__ = "reservations"
    __table_args__ = (UniqueConstraint("job_id"), ForeignKeyConstraint(["workspace_id", "job_id"], [f"{SCHEMA}.jobs.workspace_id", f"{SCHEMA}.jobs.id"]), {"schema": SCHEMA})
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    bytes: Mapped[int] = mapped_column(BigInteger)


class Recovery(TenantMixin, Base):
    __tablename__ = "recoveries"
    __table_args__ = (UniqueConstraint("workspace_id", "id"),
                      ForeignKeyConstraint(["workspace_id", "snapshot_id"], [f"{SCHEMA}.snapshots.workspace_id", f"{SCHEMA}.snapshots.id"]), {"schema": SCHEMA})
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    target_owner: Mapped[str] = mapped_column(String(100))
    target_name: Mapped[str] = mapped_column(String(100))
    target_account_id: Mapped[int] = mapped_column(BigInteger)
    destination_connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    destination_user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(30), default="planning")
    verified_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    report: Mapped[dict] = mapped_column(JSONB, default=dict)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthState(TenantMixin, Base):
    __tablename__ = "oauth_states"
    __table_args__ = {"schema": SCHEMA}
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(TenantMixin, Base):
    __tablename__ = "events"
    __table_args__ = {"schema": SCHEMA}
    kind: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = {"schema": SCHEMA}
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
