import logging
import smtplib
import time
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import func, select

from backitup.config import get_settings
from backitup.db import session_scope
from backitup.jobs import ACTIVE, dispatch_due, reap_expired, settle_reservation
from backitup.models import (
    Event,
    Job,
    Platform,
    Snapshot,
    StorageProfile,
    Workspace,
    now,
)
from backitup.services import storage_for

logger = logging.getLogger("backitup.scheduler")


def prune_expired():
    # Claim deletion in the index before removing the storage completion marker.
    with session_scope() as session:
        candidates = session.scalars(select(Snapshot).where(Snapshot.state.in_(("complete", "pruning")), Snapshot.expires_at < now()).order_by(Snapshot.expires_at).with_for_update(skip_locked=True).limit(20)).all()
        work = []
        for snapshot in candidates:
            latest = session.scalar(select(Snapshot.id).where(Snapshot.repository_id == snapshot.repository_id, Snapshot.state == "complete").order_by(Snapshot.created_at.desc()).limit(1))
            if latest == snapshot.id:
                continue
            busy = session.scalar(select(func.count()).select_from(Job).where(Job.repository_id == snapshot.repository_id, Job.state.in_((*ACTIVE, "queued", "retry_wait")), Job.kind.in_(("recovery_plan", "recovery_apply"))))
            profile = session.get(StorageProfile, snapshot.storage_id)
            if busy or not profile:
                continue
            snapshot.state = "pruning"
            work.append((snapshot, profile))
    for snapshot, profile in work:
        try:
            storage = storage_for(profile)
            storage.delete_prefix(snapshot.object_prefix)
            if list(storage.list(snapshot.object_prefix)):
                raise RuntimeError("Objects remain after pruning")
            with session_scope() as session:
                saved = session.scalar(select(Snapshot).where(Snapshot.id == snapshot.id, Snapshot.state == "pruning").with_for_update())
                if not saved:
                    continue
                platform = session.scalar(select(Platform).where(Platform.id == 1).with_for_update())
                workspace = session.scalar(select(Workspace).where(Workspace.id == snapshot.workspace_id).with_for_update())
                platform.used_bytes = max(0, platform.used_bytes - snapshot.size_bytes)
                workspace.used_bytes = max(0, workspace.used_bytes - snapshot.size_bytes)
                saved.state = "pruned"
                session.add(Event(workspace_id=snapshot.workspace_id, kind="retention_pruned", message="Expired backup removed under the accepted retention policy", details={"snapshot_id": str(snapshot.id)}))
        except Exception as exc:
            logger.error("Retention retry required for %s: %s", snapshot.id, type(exc).__name__)


def cleanup_incomplete():
    # A failed job is reclaimable after 24h. Lock its workspace during the claim;
    # public retry checks the cleanup flag before it may make work runnable again.
    with session_scope() as session:
        candidates = session.scalars(select(Job).where(Job.kind == "backup", Job.state.in_(("failed", "cancelled")), Job.finished_at < now() - timedelta(hours=24)).with_for_update(skip_locked=True).limit(20)).all()
        work = []
        for job in candidates:
            if job.payload.get("cleanup_complete"):
                continue
            profile = session.get(StorageProfile, job.payload.get("storage_id"))
            if not profile:
                continue
            job.payload = {**job.payload, "cleanup_in_progress": True}
            work.append((job, profile))
    for job, profile in work:
        try:
            storage = storage_for(profile)
            prefix = f"snapshots/{job.repository_id}/{job.id}"
            objects = list(storage.list(prefix))
            if any(item["path"].endswith("/complete.json") for item in objects):
                # A valid publication awaiting acknowledgement is recoverable by Retry.
                continue
            storage.delete_prefix(prefix)
            if list(storage.list(prefix)):
                continue
            with session_scope() as session:
                saved = session.get(Job, job.id)
                settle_reservation(session, saved)
                saved.payload = {**saved.payload, "cleanup_complete": True, "cleanup_in_progress": False}
        except Exception as exc:
            logger.error("Incomplete cleanup retry required for %s: %s", job.id, type(exc).__name__)
        finally:
            with session_scope() as session:
                saved = session.get(Job, job.id)
                saved.payload = {**saved.payload, "cleanup_in_progress": False}


def send_notifications():
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from:
        return
    with session_scope() as session:
        rows = session.execute(select(Event, Workspace).join(Workspace, Event.workspace_id == Workspace.id).where(Event.email_sent.is_(False), Event.kind.in_(("job_failed", "missed_schedule", "github_revoked")), Workspace.recovery_verified_at.is_not(None)).order_by(Event.created_at).limit(20)).all()
    for event, workspace in rows:
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = workspace.email
        message["Subject"] = "BackItUp: your backups need attention"
        message.set_content(f"{event.message}\n\nOpen your dashboard for details: {settings.frontend_url}\n")
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
            with session_scope() as session:
                session.get(Event, event.id).email_sent = True
        except Exception as exc:
            logger.error("Notification delivery pending: %s", type(exc).__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    settings.validate_production()
    maintenance_at = 0
    while True:
        try:
            with session_scope() as session:
                # Only one dispatcher performs a tick; workers use a separate short lock.
                from sqlalchemy import text
                locked = session.scalar(text("SELECT pg_try_advisory_xact_lock(7843341202)"))
                if locked:
                    reap_expired(session, settings)
                    dispatch_due(session, settings)
            if time.monotonic() >= maintenance_at:
                prune_expired()
                cleanup_incomplete()
                send_notifications()
                maintenance_at = time.monotonic() + 60
        except Exception as exc:
            logger.error("Scheduler tick failed: %s", type(exc).__name__)
        time.sleep(10)


if __name__ == "__main__":
    main()
