import hashlib
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlencode

import jwt
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from backitup.auth import auth_user, bearer, current_workspace, internal_auth
from backitup.config import get_settings
from backitup.db import get_session, session_scope
from backitup.errors import BackItUpError
from backitup.github import ALL_CATEGORIES, GitHub, exchange_code, valid_owner
from backitup.jobs import ACTIVE, PENDING, enqueue, heartbeat, lease_job, serialize_job
from backitup.models import (
    Connection,
    Event,
    Installation,
    Job,
    OAuthState,
    Plan,
    Recovery,
    Repository,
    Snapshot,
    StorageProfile,
    WebhookDelivery,
    Worker,
    Workspace,
    now,
)
from backitup.schemas import (
    ApplyInput,
    HeartbeatInput,
    PlanInput,
    RecoveryInput,
    SelectRepository,
    WorkerInput,
)
from backitup.security import opaque_token, token_hash
from backitup.services import cipher, github_for, managed_storage_for, scoped, storage_for

logger = logging.getLogger("backitup")


@asynccontextmanager
async def lifespan(app):
    get_settings().validate_production()
    yield


app = FastAPI(title="BackItUp", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[get_settings().frontend_url], allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])


@app.exception_handler(BackItUpError)
async def known_error(request, error):
    return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)


@app.exception_handler(Exception)
async def unknown_error(request, error):
    # Avoid raw third-party exceptions, database parameters, and credential-bearing URLs.
    logger.error("Request failed: %s on %s", type(error).__name__, request.url.path)
    return JSONResponse({"error": {"code": "internal_error", "message": "The service could not complete this request. Please retry."}}, status_code=500)


def require_ready(workspace):
    if not workspace.recovery_verified_at:
        raise BackItUpError("recovery_email_required", "Verify your emergency sign-in email before starting backups", status=409)


def idempotency(value: str | None) -> str:
    if not value or not 8 <= len(value) <= 100 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise BackItUpError("idempotency_required", "Provide a request identifier between 8 and 100 printable characters")
    return value


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/v1/config")
def public_config():
    settings = get_settings()
    return {"supabase_url": settings.supabase_public_url, "supabase_publishable_key": settings.supabase_publishable_key,
            "source_install_url": f"https://github.com/apps/{settings.source_app_slug}/installations/new" if settings.source_app_slug else None,
            "recovery_install_url": f"https://github.com/apps/{settings.recovery_app_slug}/installations/new" if settings.recovery_app_slug else None,
            "categories": list(ALL_CATEGORIES), "workspace_storage_limit": settings.managed_workspace_bytes,
            "schedule_intervals": [0, 6, 24]}


@app.get("/api/v1/account")
def account(workspace=Depends(current_workspace), session=Depends(get_session)):
    connections = session.scalars(select(Connection).where(Connection.workspace_id == workspace.id)).all()
    return {"workspace_id": str(workspace.id), "email": workspace.email, "recovery_email_verified": bool(workspace.recovery_verified_at),
            "connections": {c.purpose: {"id": str(c.id), "login": c.login, "provider_user_id": c.provider_user_id} for c in connections}}


@app.post("/api/v1/account/recovery-email/verify")
def verify_recovery_email(workspace=Depends(current_workspace), credentials=Depends(bearer), session=Depends(get_session)):
    # Token signature was already validated by current_workspace. AMR is an Auth-server claim.
    claims = jwt.decode(credentials.credentials, options={"verify_signature": False})
    otp = [v for v in claims.get("amr", []) if v.get("method") in ("otp", "magiclink") and v.get("timestamp", 0) > now().timestamp() - 900]
    if not otp:
        raise BackItUpError("email_proof_required", "Open a fresh email sign-in link to verify recovery access", status=409)
    user = auth_user(credentials.credentials)
    if user["id"] != str(workspace.owner_id) or not user.get("email_confirmed_at") or not user.get("email"):
        raise BackItUpError("email_proof_required", "Confirm the email for this account first", status=409)
    saved = session.get(Workspace, workspace.id)
    saved.email = user["email"]
    saved.recovery_verified_at = now()
    session.add(Event(workspace_id=workspace.id, kind="recovery_email_verified", message="Emergency email sign-in verified"))
    session.commit()
    return {"verified": True, "email": saved.email}


@app.get("/api/v1/overview")
def overview(workspace=Depends(current_workspace), session=Depends(get_session)):
    settings = get_settings()
    jobs = session.scalars(select(Job).where(Job.workspace_id == workspace.id).order_by(Job.created_at.desc()).limit(12)).all()
    online = session.scalar(select(func.count()).select_from(Worker).where(Worker.revoked.is_(False), Worker.last_seen > now() - timedelta(seconds=settings.lease_seconds)))
    return {"repositories": session.scalar(select(func.count()).select_from(Repository).where(Repository.workspace_id == workspace.id)),
            "complete_snapshots": session.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.workspace_id == workspace.id, Snapshot.state == "complete")),
            "active_jobs": session.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace.id, Job.state.in_((*ACTIVE, *PENDING)))),
            "managed_used_bytes": workspace.used_bytes, "managed_reserved_bytes": workspace.reserved_bytes,
            "managed_limit_bytes": settings.managed_workspace_bytes, "service_available": bool(online),
            "jobs": [serialize_job(v) for v in jobs]}


@app.get("/api/v1/github/{purpose}/connect")
def connect_github(purpose: str, workspace=Depends(current_workspace), session=Depends(get_session)):
    if purpose not in ("source", "recovery"):
        raise BackItUpError("not_found", "Unknown GitHub connection", status=404)
    settings = get_settings()
    client_id = getattr(settings, f"{purpose}_client_id")
    if not client_id:
        raise BackItUpError("github_setup", f"The operator must configure the {purpose} GitHub App", status=503)
    token = opaque_token()
    session.add(OAuthState(workspace_id=workspace.id, token_hash=token_hash(token), purpose=purpose, expires_at=now() + timedelta(minutes=10)))
    session.commit()
    callback = settings.public_url + f"/api/v1/github/{purpose}/callback"
    return {"url": "https://github.com/login/oauth/authorize?" + urlencode({"client_id": client_id, "redirect_uri": callback, "state": token})}


@app.get("/api/v1/github/{purpose}/callback")
def github_callback(purpose: str, state: str = "", code: str = "", error: str = ""):
    settings = get_settings()
    if purpose not in ("source", "recovery"):
        raise BackItUpError("not_found", "Unknown GitHub connection", status=404)
    with session_scope() as session:
        saved = session.scalar(select(OAuthState).where(OAuthState.token_hash == token_hash(state), OAuthState.purpose == purpose).with_for_update())
        if not saved or saved.used or saved.expires_at <= now():
            raise BackItUpError("oauth_state", "GitHub connection expired or was already used. Start again.")
        saved.used = True
        workspace_id = saved.workspace_id
    if error or not code:
        return RedirectResponse(settings.frontend_url + "/?connection_error=authorization_cancelled")
    try:
        data = exchange_code(getattr(settings, f"{purpose}_client_id"), getattr(settings, f"{purpose}_client_secret"), code,
                             settings.public_url + f"/api/v1/github/{purpose}/callback")
        gh = GitHub(data["access_token"], settings.github_api_version)
        try:
            identity = gh.get("/user")
        finally:
            gh.close()
        with session_scope() as session:
            connection = session.scalar(select(Connection).where(Connection.workspace_id == workspace_id, Connection.purpose == purpose).with_for_update())
            if not connection:
                connection = Connection(workspace_id=workspace_id, purpose=purpose)
                session.add(connection)
            connection.provider_user_id = identity["id"]
            connection.login = identity["login"]
            connection.encrypted_token = cipher().encrypt(data)
            session.add(Event(workspace_id=workspace_id, kind="github_connected", message=f"GitHub {purpose} account authorized", details={"login": identity["login"]}))
        return RedirectResponse(settings.frontend_url + f"/?connected={purpose}")
    except BackItUpError as exc:
        return RedirectResponse(settings.frontend_url + "/?" + urlencode({"connection_error": exc.code}))


def discover(workspace_id):
    settings = get_settings()
    result = []
    with github_for(workspace_id, "source") as (gh, _):
        installations = list(gh.pages("/user/installations", key="installations"))
        for installation in installations:
            if str(installation["app_id"]) != settings.source_app_id or installation.get("suspended_at"):
                continue
            for repo in gh.pages(f"/user/installations/{installation['id']}/repositories", key="repositories"):
                result.append({"provider_id": repo["id"], "installation_id": installation["id"], "owner": repo["owner"]["login"], "name": repo["name"],
                               "private": repo["private"], "default_branch": repo.get("default_branch", "main"), "size_kb": repo.get("size", 0), "installation": installation})
    return result


@app.get("/api/v1/github/repositories")
def discover_repositories(workspace=Depends(current_workspace)):
    return [{k: v for k, v in r.items() if k != "installation"} for r in discover(workspace.id)]


@app.post("/api/v1/repositories")
def select_repository(data: SelectRepository, workspace=Depends(current_workspace), session=Depends(get_session)):
    found = next((r for r in discover(workspace.id) if r["provider_id"] == data.provider_id and r["installation_id"] == data.installation_id), None)
    if not found:
        raise BackItUpError("github_permissions", "This repository is not authorized by your GitHub installation", status=403)
    installation = found["installation"]
    statement = insert(Installation).values(workspace_id=workspace.id, provider_id=data.installation_id, account_id=installation["account"]["id"], account_login=installation["account"]["login"], permissions=installation.get("permissions", {}))
    session.execute(statement.on_conflict_do_update(index_elements=[Installation.workspace_id, Installation.provider_id], set_={"permissions": installation.get("permissions", {})}))
    saved_install = session.scalar(select(Installation).where(Installation.workspace_id == workspace.id, Installation.provider_id == data.installation_id))
    statement = insert(Repository).values(workspace_id=workspace.id, provider_id=data.provider_id, installation_id=saved_install.id, owner=found["owner"], name=found["name"], private=found["private"], default_branch=found["default_branch"])
    session.execute(statement.on_conflict_do_update(index_elements=[Repository.workspace_id, Repository.provider_id], set_={"owner": found["owner"], "name": found["name"], "installation_id": saved_install.id}))
    saved = session.scalar(select(Repository).where(Repository.workspace_id == workspace.id, Repository.provider_id == data.provider_id))
    session.commit()
    return {"id": str(saved.id), "owner": saved.owner, "name": saved.name}


def plan_public(plan):
    return {"id": str(plan.id), "repository_id": str(plan.repository_id), "interval_hours": plan.interval_hours,
            "retention_days": plan.retention_days, "categories": plan.categories, "enabled": plan.enabled, "next_due": plan.next_due}


@app.get("/api/v1/repositories")
def repositories(workspace=Depends(current_workspace), session=Depends(get_session)):
    records = session.scalars(select(Repository).where(Repository.workspace_id == workspace.id).order_by(Repository.created_at)).all()
    result = []
    for repo in records:
        plan = session.scalar(select(Plan).where(Plan.repository_id == repo.id, Plan.workspace_id == workspace.id))
        attempted = session.scalar(select(Job).where(Job.repository_id == repo.id, Job.workspace_id == workspace.id, Job.kind == "backup").order_by(Job.created_at.desc()).limit(1))
        complete = session.scalar(select(Snapshot).where(Snapshot.repository_id == repo.id, Snapshot.workspace_id == workspace.id, Snapshot.state == "complete").order_by(Snapshot.created_at.desc()).limit(1))
        result.append({"id": str(repo.id), "provider_id": repo.provider_id, "owner": repo.owner, "name": repo.name, "private": repo.private,
                       "plan": plan_public(plan) if plan else None, "last_attempt": serialize_job(attempted) if attempted else None,
                       "last_complete": complete.created_at if complete else None})
    return result


@app.get("/api/v1/storage")
def storage_status(workspace=Depends(current_workspace), session=Depends(get_session)):
    settings = get_settings()
    if not settings.managed_storage_ready():
        return {"available": False, "storage": None, "message": "BackItUp Cloud is being configured"}
    return {"available": True, "storage": {"name": "BackItUp Cloud", "provider": "BackItUp Cloud"}, "message": None}


@app.put("/api/v1/plans")
def save_plan(data: PlanInput, workspace=Depends(current_workspace), session=Depends(get_session)):
    scoped(session, Repository, data.repository_id, workspace.id)
    profile = managed_storage_for(session, workspace)
    plan = session.scalar(select(Plan).where(Plan.repository_id == data.repository_id, Plan.workspace_id == workspace.id).with_for_update())
    if not plan:
        plan = Plan(workspace_id=workspace.id)
        session.add(plan)
    for key, value in data.model_dump().items():
        setattr(plan, key, value)
    plan.storage_id = profile.id
    plan.next_due = now() + timedelta(hours=data.interval_hours) if data.enabled and data.interval_hours else None
    session.add(Event(workspace_id=workspace.id, kind="plan_updated", message="Backup schedule and retention saved", details={"repository_id": str(data.repository_id), "retention_days": data.retention_days}))
    session.commit()
    return plan_public(plan)


@app.post("/api/v1/repositories/{repository_id}/backup", status_code=202)
def manual_backup(repository_id: uuid.UUID, idempotency_key: str | None = Header(default=None), workspace=Depends(current_workspace), session=Depends(get_session)):
    scoped(session, Repository, repository_id, workspace.id)
    plan = session.scalar(select(Plan).where(Plan.workspace_id == workspace.id, Plan.repository_id == repository_id))
    if not plan:
        raise BackItUpError("plan_required", "Save a backup plan first", status=409)
    job = enqueue(session, workspace_id=workspace.id, repository_id=repository_id, plan_id=plan.id, key="manual:" + idempotency(idempotency_key),
                  payload={"storage_id": str(plan.storage_id), "categories": plan.categories, "retention_days": plan.retention_days}, settings=get_settings())
    session.commit()
    return serialize_job(job)


@app.get("/api/v1/jobs")
def jobs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), workspace=Depends(current_workspace), session=Depends(get_session)):
    return [serialize_job(job) for job in session.scalars(select(Job).where(Job.workspace_id == workspace.id).order_by(Job.created_at.desc()).offset(offset).limit(limit))]


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, workspace=Depends(current_workspace), session=Depends(get_session)):
    job = scoped(session, Job, job_id, workspace.id)
    if job.state in PENDING:
        job.state = "cancelled"
        job.finished_at = now()
    elif job.state in ACTIVE:
        job.cancel_requested = True
    session.commit()
    return serialize_job(job)


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: uuid.UUID, workspace=Depends(current_workspace), session=Depends(get_session)):
    job = scoped(session, Job, job_id, workspace.id)
    if job.state not in ("failed", "cancelled"):
        raise BackItUpError("job_active", "This job is not eligible for retry", status=409)
    job.state = "retry_wait"
    job.available_at = now()
    job.cancel_requested = False
    job.attempt = 0
    job.error_code = job.error_message = None
    job.finished_at = None
    session.commit()
    return serialize_job(job)


def snapshot_public(snapshot, repo=None):
    return {"id": str(snapshot.id), "repository_id": str(snapshot.repository_id), "created_at": snapshot.created_at,
            "expires_at": snapshot.expires_at, "size_bytes": snapshot.size_bytes, "coverage": snapshot.coverage, "summary": snapshot.manifest_summary,
            "state": snapshot.state, "repository": f"{repo.owner}/{repo.name}" if repo else None}


@app.get("/api/v1/snapshots")
def snapshots(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), workspace=Depends(current_workspace), session=Depends(get_session)):
    rows = session.execute(select(Snapshot, Repository).join(Repository, Repository.id == Snapshot.repository_id).where(Snapshot.workspace_id == workspace.id, Snapshot.state == "complete").order_by(Snapshot.created_at.desc()).offset(offset).limit(limit)).all()
    return [snapshot_public(snapshot, repo) for snapshot, repo in rows]


@app.get("/api/v1/snapshots/{snapshot_id}")
def inspect_snapshot(snapshot_id: uuid.UUID, workspace=Depends(current_workspace), session=Depends(get_session)):
    snapshot = scoped(session, Snapshot, snapshot_id, workspace.id)
    return snapshot_public(snapshot, scoped(session, Repository, snapshot.repository_id, workspace.id))


@app.get("/api/v1/snapshots/{snapshot_id}/download")
def download_snapshot(snapshot_id: uuid.UUID, workspace=Depends(current_workspace), session=Depends(get_session)):
    """Issue a short-lived archive link only after rechecking its signed completion record."""
    from backitup.artifacts import verify_manifest
    from backitup.security import ManifestSigner

    snapshot = scoped(session, Snapshot, snapshot_id, workspace.id)
    if snapshot.state != "complete":
        raise BackItUpError("snapshot_incomplete", "Only complete snapshots can be downloaded", status=409)
    repository = scoped(session, Repository, snapshot.repository_id, workspace.id)
    profile = scoped(session, StorageProfile, snapshot.storage_id, workspace.id)
    storage = storage_for(profile)
    manifest, _ = verify_manifest(
        storage,
        snapshot.object_prefix,
        ManifestSigner(get_settings().signing_key_file, get_settings().verification_keys_dir),
        workspace_id=str(workspace.id),
        repository_id=str(repository.id),
        job_id=str(snapshot.job_id),
        expected_digest=snapshot.manifest_sha256,
    )
    filename = f"{repository.name}-snapshot-{snapshot.created_at.date().isoformat()}.tar.zst"
    return {"url": storage.download_url(manifest["archive"]["path"], filename)}


@app.get("/api/v1/snapshots/{snapshot_id}/metadata/{category}")
def download_metadata(snapshot_id: uuid.UUID, category: str, workspace=Depends(current_workspace)):
    from backitup.worker import download_metadata_file
    result = download_metadata_file(workspace.id, snapshot_id, category)
    return Response(result, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{category}.json"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/v1/recoveries", status_code=202)
def plan_recovery(data: RecoveryInput, workspace=Depends(current_workspace), session=Depends(get_session)):
    require_ready(workspace)
    snapshot = scoped(session, Snapshot, data.snapshot_id, workspace.id)
    if snapshot.state != "complete":
        raise BackItUpError("snapshot_incomplete", "Only complete snapshots can be recovered", status=409)
    # Network operations occur before locking any job rows.
    with github_for(workspace.id, "recovery") as (gh, connection):
        user = gh.get("/user")
        if user["id"] != connection.provider_user_id:
            raise BackItUpError("destination_identity", "Destination account changed; reconnect it", status=409)
        owner = gh.get(f"/users/{valid_owner(data.target_owner)}")
        if owner["type"] != "Organization" and owner["id"] != user["id"]:
            raise BackItUpError("destination_owner", "Choose your authorized personal account or organization", status=403)
        if owner["type"] == "Organization":
            membership = gh.get(f"/user/memberships/orgs/{data.target_owner}")
            if membership.get("state") != "active":
                raise BackItUpError("destination_owner", "Active organization membership is required", status=403)
        if gh.get(f"/repos/{data.target_owner}/{data.target_name}", allow_not_found=True):
            raise BackItUpError("name_collision", "A repository already has this name. Choose another name.", status=409)
        connection_id = connection.id
    recovery = Recovery(workspace_id=workspace.id, snapshot_id=snapshot.id, target_owner=data.target_owner,
                        target_name=data.target_name, target_account_id=owner["id"], destination_connection_id=connection_id, destination_user_id=user["id"])
    session.add(recovery)
    session.flush()
    job = enqueue(session, workspace_id=workspace.id, repository_id=snapshot.repository_id, kind="recovery_plan", key=f"plan:{recovery.id}",
                  payload={"recovery_id": str(recovery.id)}, settings=get_settings())
    session.commit()
    return {"id": str(recovery.id), "job": serialize_job(job)}


def recovery_public(recovery):
    return {"id": str(recovery.id), "snapshot_id": str(recovery.snapshot_id), "target_owner": recovery.target_owner,
            "target_name": recovery.target_name, "status": recovery.status, "plan": recovery.verified_plan,
            "report": recovery.report, "checkpoint": recovery.checkpoint, "created_at": recovery.created_at}


@app.get("/api/v1/recoveries")
def recoveries(workspace=Depends(current_workspace), session=Depends(get_session)):
    return [recovery_public(v) for v in session.scalars(select(Recovery).where(Recovery.workspace_id == workspace.id).order_by(Recovery.created_at.desc()).limit(100))]


@app.post("/api/v1/recoveries/{recovery_id}/apply", status_code=202)
def apply_recovery(recovery_id: uuid.UUID, data: ApplyInput, idempotency_key: str | None = Header(default=None), workspace=Depends(current_workspace), session=Depends(get_session)):
    recovery = scoped(session, Recovery, recovery_id, workspace.id)
    if recovery.status not in ("ready", "failed", "restoring", "succeeded") or not recovery.verified_plan or data.plan_digest != recovery.verified_plan.get("digest"):
        raise BackItUpError("plan_changed", "Review a valid recovery plan before confirming", status=409)
    snapshot = scoped(session, Snapshot, recovery.snapshot_id, workspace.id)
    job = enqueue(session, workspace_id=workspace.id, repository_id=snapshot.repository_id, kind="recovery_apply",
                  key=f"apply:{recovery.id}:{idempotency(idempotency_key)}", payload={"recovery_id": str(recovery.id)}, settings=get_settings())
    if recovery.status != "succeeded":
        recovery.approved_at = now()
        recovery.status = "restoring"
    session.commit()
    return serialize_job(job)


@app.get("/api/v1/events")
def events(workspace=Depends(current_workspace), session=Depends(get_session)):
    return [{"id": str(v.id), "kind": v.kind, "message": v.message, "details": v.details, "read": v.read, "created_at": v.created_at} for v in session.scalars(select(Event).where(Event.workspace_id == workspace.id).order_by(Event.created_at.desc()).limit(100))]


@app.post("/api/v1/events/{event_id}/read")
def read_event(event_id: uuid.UUID, workspace=Depends(current_workspace), session=Depends(get_session)):
    event = scoped(session, Event, event_id, workspace.id)
    event.read = True
    session.commit()
    return {"read": True}


@app.post("/api/v1/github/webhook")
async def github_webhook(request: Request, x_hub_signature_256: str = Header(default=""), x_github_delivery: str = Header(default=""), x_github_event: str = Header(default="")):
    settings = get_settings()
    body = await request.body()
    if len(body) > 2_000_000 or not settings.source_webhook_secret or not x_github_delivery or len(x_github_delivery) > 100:
        raise BackItUpError("webhook_invalid", "Invalid webhook", status=400)
    expected = "sha256=" + hmac.new(settings.source_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise BackItUpError("webhook_invalid", "Webhook signature did not match", status=401)
    data = json.loads(body)
    with session_scope() as session:
        new = session.execute(insert(WebhookDelivery).values(id=x_github_delivery).on_conflict_do_nothing().returning(WebhookDelivery.id)).scalar()
        if not new:
            return {"duplicate": True}
        if x_github_event == "installation" and data.get("action") in ("deleted", "suspend"):
            for installation in session.scalars(select(Installation).where(Installation.provider_id == data.get("installation", {}).get("id"))):
                for plan in session.scalars(select(Plan).join(Repository, Repository.id == Plan.repository_id).where(Repository.installation_id == installation.id)):
                    plan.enabled = False
                session.add(Event(workspace_id=installation.workspace_id, kind="github_revoked", message="Source GitHub installation was removed or suspended. Existing backups remain available."))
    return {"received": True}


@app.post("/internal/workers/register", dependencies=[Depends(internal_auth)])
def register_worker(data: WorkerInput, session=Depends(get_session)):
    session.execute(insert(Worker).values(name=data.name).on_conflict_do_nothing(index_elements=[Worker.name]))
    worker = session.scalar(select(Worker).where(Worker.name == data.name))
    if worker.revoked:
        raise BackItUpError("worker_revoked", "Worker is revoked", status=403)
    worker.last_seen = now()
    session.commit()
    return {"id": str(worker.id)}


@app.post("/internal/workers/{worker_id}/lease", dependencies=[Depends(internal_auth)])
def lease(worker_id: uuid.UUID, session=Depends(get_session)):
    worker = session.get(Worker, worker_id)
    if not worker:
        raise BackItUpError("not_found", "Worker not registered", status=404)
    job = lease_job(session, worker, get_settings())
    result = {**serialize_job(job), "lease_version": job.lease_version, "payload": job.payload} if job else None
    session.commit()
    return result


@app.post("/internal/jobs/{job_id}/heartbeat", dependencies=[Depends(internal_auth)])
def worker_heartbeat(job_id: uuid.UUID, data: HeartbeatInput, session=Depends(get_session)):
    cancelled = heartbeat(session, job_id, data.worker_id, data.lease_version, get_settings(), {"stage": data.stage})
    session.commit()
    return {"cancel_requested": cancelled}
