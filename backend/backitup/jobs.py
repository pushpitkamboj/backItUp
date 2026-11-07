import random
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backitup.config import Settings
from backitup.errors import BackItUpError, redact
from backitup.models import Event, Job, Plan, Platform, Reservation, Worker, Workspace, now

ACTIVE = ("leased", "running", "committing")
PENDING = ("queued", "retry_wait")
TERMINAL = ("succeeded", "failed", "cancelled")


def serialize_job(job: Job) -> dict:
    return {"id": str(job.id), "repository_id": str(job.repository_id), "kind": job.kind,
            "state": job.state, "attempt": job.attempt, "due_at": job.due_at,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "progress": job.progress, "result": job.result,
            "error_code": job.error_code, "error_message": job.error_message,
            "cancel_requested": job.cancel_requested}


def enqueue(session: Session, *, workspace_id: uuid.UUID, repository_id: uuid.UUID, key: str,
            kind: str = "backup", plan_id=None, payload=None, due_at=None, settings: Settings | None = None) -> Job:
    # The workspace lock serializes public submissions and rate/admission decisions.
    session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    existing = session.scalar(select(Job).where(Job.workspace_id == workspace_id, Job.idempotency_key == key))
    if existing:
        if existing.kind != kind or existing.repository_id != repository_id:
            raise BackItUpError("idempotency_conflict", "This request key belongs to different work", status=409)
        return existing
    if settings:
        queued = session.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace_id, Job.state.in_(PENDING)))
        if queued >= settings.max_queued_per_workspace:
            raise BackItUpError("queue_limit", "Too much work is already queued; let existing jobs finish", status=429)
        if key.startswith("manual:"):
            recent = session.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace_id, Job.kind == "backup", Job.idempotency_key.like("manual:%"), Job.created_at > now() - timedelta(hours=1)))
            if recent >= settings.manual_backups_per_hour:
                raise BackItUpError("rate_limit", "Manual backup limit reached; try again later", status=429)
    job = Job(workspace_id=workspace_id, repository_id=repository_id, plan_id=plan_id,
              kind=kind, priority=0 if kind.startswith("recovery") else 10,
              due_at=due_at or now(), available_at=now(), idempotency_key=key, payload=payload or {})
    session.add(job)
    session.flush()
    return job


def dispatch_due(session: Session, settings: Settings) -> int:
    timestamp = now()
    plans = session.scalars(select(Plan).where(Plan.enabled.is_(True), Plan.interval_hours > 0, Plan.next_due <= timestamp).with_for_update(skip_locked=True).limit(100)).all()
    count = 0
    for plan in plans:
        interval = timedelta(hours=plan.interval_hours)
        due = plan.next_due
        missed = int((timestamp - due) // interval)
        # A catch-up backup captures current data, never pretends to capture a past point in time.
        slot = due + missed * interval
        try:
            enqueue(session, workspace_id=plan.workspace_id, repository_id=plan.repository_id,
                    plan_id=plan.id, key=f"scheduled:{plan.id}:{slot.isoformat()}", due_at=slot,
                    payload={"storage_id": str(plan.storage_id), "categories": plan.categories, "retention_days": plan.retention_days}, settings=settings)
            count += 1
        except BackItUpError:
            missed += 1
        if missed:
            session.add(Event(workspace_id=plan.workspace_id, kind="missed_schedule", message=f"{missed} scheduled run(s) were missed or delayed", details={"repository_id": str(plan.repository_id), "first_due": due.isoformat()}))
        plan.next_due = slot + interval
    return count


def lease_job(session: Session, worker: Worker, settings: Settings) -> Job | None:
    if worker.revoked:
        raise BackItUpError("worker_revoked", "Worker is revoked", status=403)
    # Short global admission lock protects concurrency counts; transfers never hold it.
    session.execute(text("SELECT pg_advisory_xact_lock(7843341201)"))
    worker.last_seen = now()
    active = session.scalar(select(func.count()).select_from(Job).where(Job.state.in_(ACTIVE)))
    if active >= settings.global_concurrency:
        return None
    jobs = session.scalars(select(Job).where(Job.state.in_(PENDING), Job.available_at <= now(), Job.cancel_requested.is_(False)).order_by(Job.priority, Job.available_at, Job.created_at).with_for_update(skip_locked=True).limit(100)).all()
    for job in jobs:
        busy = session.scalar(select(func.count()).select_from(Job).where(Job.state.in_(ACTIVE), Job.workspace_id == job.workspace_id))
        repository_busy = session.scalar(select(func.count()).select_from(Job).where(Job.state.in_(ACTIVE), Job.repository_id == job.repository_id))
        if busy >= settings.workspace_concurrency or repository_busy:
            continue
        job.state = "leased"
        job.worker_id = worker.id
        job.attempt += 1
        job.lease_version += 1
        job.lease_until = now() + timedelta(seconds=settings.lease_seconds)
        job.started_at = now()
        job.progress = {"stage": "starting"}
        session.flush()
        return job
    return None


def require_lease(session: Session, job_id, worker_id, version: int) -> Job:
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    worker = session.get(Worker, worker_id)
    if not job or not worker or worker.revoked or job.worker_id != worker_id or job.lease_version != version or job.state not in ACTIVE or not job.lease_until or job.lease_until <= now():
        raise BackItUpError("stale_lease", "Worker lease expired or was superseded", status=409)
    return job


def heartbeat(session: Session, job_id, worker_id, version: int, settings: Settings, progress=None) -> bool:
    job = require_lease(session, job_id, worker_id, version)
    job.lease_until = now() + timedelta(seconds=settings.lease_seconds)
    session.get(Worker, worker_id).last_seen = now()
    if progress:
        job.progress = progress
    if job.state == "leased":
        job.state = "running"
    return job.cancel_requested


def fail_job(session: Session, job: Job, error: BackItUpError, settings: Settings) -> None:
    job.error_code = error.code
    job.error_message = redact(error.message)
    if error.code == "cancelled" or job.cancel_requested:
        job.state = "cancelled"
    elif error.transient and job.attempt < settings.max_attempts:
        job.state = "retry_wait"
        job.available_at = now() + timedelta(seconds=min(300, 10 * 2 ** (job.attempt - 1)) + random.uniform(0, 5))
    else:
        job.state = "failed"
    job.lease_until = None
    if job.state in TERMINAL:
        job.finished_at = now()
        session.add(Event(workspace_id=job.workspace_id, kind="job_failed", message=job.error_message, details={"job_id": str(job.id), "code": error.code}))


def reap_expired(session: Session, settings: Settings) -> int:
    jobs = session.scalars(select(Job).where(Job.state.in_(ACTIVE), Job.lease_until < now()).with_for_update(skip_locked=True)).all()
    for job in jobs:
        fail_job(session, job, BackItUpError("lease_expired", "Backup service was interrupted; retrying eligible work", transient=True), settings)
        job.lease_version += 1
    return len(jobs)


def reserve_bytes(session: Session, job: Job, amount: int, settings: Settings) -> None:
    # Always lock platform before workspace; both quota scopes use the same order.
    platform = session.scalar(select(Platform).where(Platform.id == 1).with_for_update())
    workspace = session.scalar(select(Workspace).where(Workspace.id == job.workspace_id).with_for_update())
    existing = session.scalar(select(Reservation).where(Reservation.job_id == job.id).with_for_update())
    old = existing.bytes if existing else 0
    delta = amount - old
    if workspace.used_bytes + workspace.reserved_bytes + delta > settings.managed_workspace_bytes:
        raise BackItUpError("workspace_quota", "Managed storage allowance is full. Change retention or use your own bucket.")
    if platform.used_bytes + platform.reserved_bytes + delta > settings.managed_global_bytes:
        raise BackItUpError("platform_quota", "Managed storage capacity is full; new uploads are paused")
    workspace.reserved_bytes += delta
    platform.reserved_bytes += delta
    if existing:
        existing.bytes = amount
    else:
        session.add(Reservation(workspace_id=job.workspace_id, job_id=job.id, bytes=amount))


def settle_reservation(session: Session, job: Job, *, committed_bytes: int = 0) -> None:
    platform = session.scalar(select(Platform).where(Platform.id == 1).with_for_update())
    workspace = session.scalar(select(Workspace).where(Workspace.id == job.workspace_id).with_for_update())
    reservation = session.scalar(select(Reservation).where(Reservation.job_id == job.id).with_for_update())
    if reservation:
        platform.reserved_bytes -= reservation.bytes
        workspace.reserved_bytes -= reservation.bytes
        session.delete(reservation)
    platform.used_bytes += committed_bytes
    workspace.used_bytes += committed_bytes
