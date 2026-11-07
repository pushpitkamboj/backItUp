"""Trusted hosted worker entrypoint; no customer-side executable is required."""
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backitup.artifacts import publish_snapshot, verify_manifest
from backitup.config import get_settings
from backitup.db import session_scope
from backitup.engine import Budget, Git, extract_archive, package_archive, sha256_file, write_json
from backitup.errors import BackItUpError
from backitup.github import (
    ARCHIVED_ONLY,
    CORE_METADATA,
    capture_wiki,
    collect_metadata,
    installation_token,
)
from backitup.jobs import (
    fail_job,
    heartbeat,
    lease_job,
    require_lease,
    reserve_bytes,
    settle_reservation,
)
from backitup.models import (
    Event,
    Installation,
    Job,
    Recovery,
    Repository,
    Snapshot,
    StorageProfile,
    Worker,
    now,
)
from backitup.security import ManifestSigner
from backitup.services import github_for, load_connection, scoped, storage_for

logger = logging.getLogger("backitup.worker")


class Context:
    def __init__(self, job: Job, budget: Budget):
        self.job = job
        self.budget = budget
        self.settings = get_settings()
        self.signer = ManifestSigner(self.settings.signing_key_file, self.settings.verification_keys_dir)
        self.stage = "starting"
        self.detail = ""

    def fence(self, *, committing=False):
        self.budget.check()
        with session_scope() as session:
            job = require_lease(session, self.job.id, self.job.worker_id, self.job.lease_version)
            if job.cancel_requested:
                raise BackItUpError("cancelled", "Job was cancelled")
            if committing:
                job.state = "committing"
            job.progress = {"stage": self.stage, "detail": self.detail}

    def progress(self, stage, detail=""):
        self.stage, self.detail = stage, detail
        self.fence()

    def checkpoint(self, recovery_id, **changes):
        self.fence()
        with session_scope() as session:
            require_lease(session, self.job.id, self.job.worker_id, self.job.lease_version)
            run = scoped(session, Recovery, recovery_id, self.job.workspace_id)
            run.checkpoint = {**run.checkpoint, **changes}


def finish(ctx: Context, result: dict):
    with session_scope() as session:
        job = require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        job.state = "succeeded"
        job.finished_at = now()
        job.lease_until = None
        job.progress = {"stage": "complete"}
        job.result = result
        job.error_code = job.error_message = None
        session.add(Event(workspace_id=job.workspace_id, kind="job_succeeded", message="Backup completed" if job.kind == "backup" else "Recovery operation completed", details={"job_id": str(job.id)}))


def commit_backup(ctx: Context, profile, prefix, manifest, size):
    with session_scope() as session:
        job = require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        existing = session.scalar(select(Snapshot).where(Snapshot.job_id == job.id))
        if not existing:
            snapshot = Snapshot(id=uuid.UUID(manifest["snapshot_id"]), workspace_id=job.workspace_id, repository_id=job.repository_id,
                                storage_id=profile.id, job_id=job.id, object_prefix=prefix, manifest_sha256=manifest["manifest_sha256"],
                                size_bytes=size, coverage=manifest["coverage"], manifest_summary={"source": manifest["source"], "collection_start": manifest["collection_start"],
                                    "collection_end": manifest["collection_end"], "git": manifest["git"], "lfs_count": len(manifest.get("lfs", [])), "selected_categories": manifest["selected_categories"]},
                                expires_at=now() + timedelta(days=job.payload.get("retention_days", 30)))
            session.add(snapshot)
            settle_reservation(session, job, committed_bytes=size)
        else:
            snapshot = existing
        job.state = "succeeded"
        job.finished_at = now()
        job.lease_until = None
        job.result = {"snapshot_id": str(snapshot.id), "coverage": manifest["coverage"], "size_bytes": size}
        job.progress = {"stage": "complete"}
        session.add(Event(workspace_id=job.workspace_id, kind="backup_complete", message="Complete backup stored and verified", details={"snapshot_id": str(snapshot.id)}))


def run_backup(ctx: Context):
    with session_scope() as session:
        repo = scoped(session, Repository, ctx.job.repository_id, ctx.job.workspace_id)
        profile = scoped(session, StorageProfile, uuid.UUID(ctx.job.payload["storage_id"]), ctx.job.workspace_id)
        installation = scoped(session, Installation, repo.installation_id, ctx.job.workspace_id)
    storage = storage_for(profile)
    job_prefix = f"snapshots/{repo.id}/{ctx.job.id}"
    ctx.progress("reconciling", "Checking interrupted publication")
    prior = list(storage.list(job_prefix))
    markers = [item["path"] for item in prior if item["path"].endswith("/complete.json")]
    if markers:
        prefix = sorted(markers)[0].removesuffix("/complete.json")
        manifest, size = verify_manifest(storage, prefix, ctx.signer, workspace_id=str(repo.workspace_id), repository_id=str(repo.id), job_id=str(ctx.job.id))
        storage.verify_object(manifest["archive"]["path"], ctx.budget, manifest["archive"]["size"], manifest["archive"]["sha256"])
        for item in manifest.get("metadata", {}).values():
            storage.verify_object(item["path"], ctx.budget, item["size"], item["sha256"])
        ctx.fence(committing=True)
        commit_backup(ctx, profile, prefix, manifest, size)
        return
    if prior:
        # Delete only uncommitted attempts of this logical job, never another snapshot.
        for prefix in {item["path"].rsplit("/", 1)[0] for item in prior if "/metadata/" not in item["path"]}:
            storage.delete_prefix(prefix)
        if list(storage.list(job_prefix)):
            storage.delete_prefix(job_prefix)
    with session_scope() as session:
        job = require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        settle_reservation(session, job)
    selected = ctx.job.payload["categories"]
    cached_token = {}

    def source_token():
        if cached_token and cached_token["expires_at"] > time.time() + 180:
            return cached_token["access_token"]
        ctx.fence()
        with github_for(repo.workspace_id, "source", budget=ctx.budget) as (gh, _):
            gh.require_repository(repo.owner, repo.name, repo.provider_id)
        token = installation_token(ctx.settings.source_app_id, ctx.settings.source_private_key_file, installation.provider_id, repo.provider_id, ctx.settings.github_api_version)
        from datetime import datetime
        cached_token.update(access_token=token["token"], expires_at=datetime.fromisoformat(token["expires_at"].replace("Z", "+00:00")).timestamp())
        return cached_token["access_token"]

    source_token()
    started = now()
    content = ctx.budget.root / "content"
    content.mkdir(mode=0o700)
    git = Git(ctx.budget, source_token)
    ctx.progress("capturing_git", f"{repo.owner}/{repo.name}")
    source = f"https://github.com/{repo.owner}/{repo.name}.git"
    git_info = git.capture(source, content / "repository.git")
    coverage = {"git": {"status": "complete", "mode": "recoverable", "ref_count": len(git_info["refs"])}}
    lfs = []
    if "lfs" in selected:
        ctx.progress("capturing_lfs")
        lfs = git.capture_lfs(content / "repository.git", source)
        coverage["lfs"] = {"status": "complete", "mode": "recoverable", "count": len(lfs)}
    else:
        coverage["lfs"] = {"status": "skipped", "reason": "Not selected; LFS pointers remain in Git history"}
    with github_for(repo.workspace_id, "source", budget=ctx.budget) as (gh, _):
        gh.require_repository(repo.owner, repo.name, repo.provider_id)
        metadata_coverage, assets = collect_metadata(gh, repo.owner, repo.name, selected, content, ctx.budget, ctx.progress)
    coverage.update(metadata_coverage)
    if "wiki" in selected:
        ctx.progress("capturing_wiki")
        coverage["wiki"] = capture_wiki(git, repo.owner, repo.name, content)
    else:
        coverage["wiki"] = {"status": "skipped", "reason": "Not selected"}
    git.remove_remote(content / "repository.git")
    git.run(["fsck", "--full", "--strict"], repo=content / "repository.git")
    if git.refs(content / "repository.git") != git_info["refs"]:
        raise BackItUpError("source_changed", "Captured Git references changed during validation")
    ended = now()
    write_json(content / "metadata" / "coverage.json", coverage)
    write_json(content / "lfs-inventory.json", lfs)
    write_json(content / "asset-inventory.json", assets)
    ctx.progress("packaging")
    archive = ctx.budget.root / "archive.tar.zst"
    info = package_archive(content, archive, ctx.budget)
    snapshot_id = str(uuid.uuid4())
    prefix = f"{job_prefix}/{snapshot_id}"
    metadata = {p.stem: p for p in (content / "metadata").glob("*.json")}
    manifest = {"schema_version": 1, "collector_version": "0.1.0", "snapshot_id": snapshot_id, "job_id": str(ctx.job.id), "workspace_id": str(repo.workspace_id),
                "repository_id": str(repo.id), "source": {"provider": "github", "provider_id": repo.provider_id, "owner": repo.owner, "name": repo.name},
                "collection_start": started.isoformat(), "collection_end": ended.isoformat(), "git": git_info, "lfs": lfs, "assets": assets,
                "coverage": coverage, "selected_categories": selected,
                "limitations": ["API and Git data cover a collection window, not an atomic point in time", "GitHub metadata is archived, not reconstructed", "GitHub-managed internal refs are excluded"],
                "compression": "zstd", "encryption": "provider-managed-at-rest", "archive": {**info, "path": prefix + "/archive.tar.zst"},
                "metadata": {key: {"path": prefix + f"/metadata/{key}.json", "size": path.stat().st_size, "sha256": sha256_file(path, ctx.budget)} for key, path in metadata.items()}}
    if any(coverage.get(category, {}).get("status") != "complete" for category in selected):
        raise BackItUpError("incomplete_coverage", "A required category is incomplete")
    estimate = info["size"] + sum(v["size"] for v in manifest["metadata"].values()) + len(json.dumps(manifest).encode()) + 64_000
    with session_scope() as session:
        job = require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        reserve_bytes(session, job, estimate, ctx.settings)
    ctx.progress("uploading", "Uploading and reading back artifacts")
    manifest, size = publish_snapshot(storage, prefix, archive, manifest, metadata, ctx.signer, ctx.budget, before_marker=lambda: ctx.fence(committing=True))
    commit_backup(ctx, profile, prefix, manifest, size)


def load_snapshot(workspace_id, snapshot_id, signer):
    with session_scope() as session:
        snapshot = scoped(session, Snapshot, snapshot_id, workspace_id)
        if snapshot.state != "complete":
            raise BackItUpError("snapshot_incomplete", "Snapshot is incomplete or being pruned")
        profile = scoped(session, StorageProfile, snapshot.storage_id, workspace_id)
    storage = storage_for(profile)
    manifest, _ = verify_manifest(storage, snapshot.object_prefix, signer, workspace_id=str(workspace_id), repository_id=str(snapshot.repository_id), expected_digest=snapshot.manifest_sha256)
    return snapshot, storage, manifest


def prepare_contents(ctx, run):
    snapshot, storage, manifest = load_snapshot(ctx.job.workspace_id, run.snapshot_id, ctx.signer)
    archive = ctx.budget.root / "download.tar.zst"
    info = manifest["archive"]
    storage.download(info["path"], archive, ctx.budget, expected_size=info["size"], expected_sha256=info["sha256"])
    destination = ctx.budget.root / "restored"
    extract_archive(archive, destination, ctx.budget, max_entries=ctx.settings.max_archive_entries)
    repo = destination / "repository.git"
    git = Git(ctx.budget)
    # A signed archive still uses a locally rebuilt Git configuration at restore time.
    config = repo / "config"
    config.write_text("[core]\n\trepositoryformatversion = " + ("0" if manifest["git"]["object_format"] == "sha1" else "1") + "\n\tbare = true\n" + ("" if manifest["git"]["object_format"] == "sha1" else f'[extensions]\n\tobjectFormat = {manifest["git"]["object_format"]}\n'))
    git.run(["fsck", "--full", "--strict"], repo=repo)
    if git.refs(repo) != manifest["git"]["refs"]:
        raise BackItUpError("ref_mismatch", "Extracted Git references do not match the manifest")
    for item in manifest.get("lfs", []):
        oid = item["oid"]
        if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
            raise BackItUpError("lfs_inventory", "Invalid LFS inventory")
        path = repo / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        if not path.is_file() or path.stat().st_size != item["size"] or sha256_file(path, ctx.budget) != oid:
            raise BackItUpError("lfs_missing", "An archived LFS object is missing or corrupt")
    return snapshot, manifest, repo


def destination_preflight(gh, connection, run, manifest, settings, *, allow_existing=False):
    user = gh.get("/user")
    if connection.id != run.destination_connection_id or user["id"] != run.destination_user_id or user["id"] != connection.provider_user_id:
        raise BackItUpError("destination_identity", "The authorized destination account changed. Create a new recovery plan.")
    owner = gh.get(f"/users/{run.target_owner}")
    if owner["id"] != run.target_account_id:
        raise BackItUpError("destination_identity", "Destination ownership changed")
    if owner["type"] == "Organization":
        membership = gh.get(f"/user/memberships/orgs/{run.target_owner}")
        if membership.get("state") != "active":
            raise BackItUpError("destination_permissions", "Active organization membership is required")
    elif owner["id"] != user["id"]:
        raise BackItUpError("destination_permissions", "You cannot create repositories in this personal account")
    installs = list(gh.pages("/user/installations", key="installations"))
    installation = next((i for i in installs if str(i["app_id"]) == settings.recovery_app_id and i["account"]["id"] == owner["id"] and not i.get("suspended_at")), None)
    if not installation:
        raise BackItUpError("recovery_installation", "Install the separate Recovery GitHub App in the destination account, then retry planning")
    permissions = installation.get("permissions", {})
    required = {"administration": "write", "contents": "write"}
    if manifest["git"].get("has_workflows"):
        required["workflows"] = "write"
    missing = [key for key, value in required.items() if permissions.get(key) != value]
    if missing:
        raise BackItUpError("recovery_permissions", "Destination App requires write permissions for: " + ", ".join(missing))
    existing = gh.get(f"/repos/{run.target_owner}/{run.target_name}", allow_not_found=True)
    if existing and not allow_existing:
        raise BackItUpError("name_collision", "This repository name is already used. Choose another name.")
    return {"identity": {"id": user["id"], "login": user["login"]}, "owner": {"id": owner["id"], "login": owner["login"], "type": owner["type"]}, "permissions": required, "installation_id": installation["id"]}


def run_recovery_plan(ctx):
    with session_scope() as session:
        run = scoped(session, Recovery, uuid.UUID(ctx.job.payload["recovery_id"]), ctx.job.workspace_id)
    ctx.progress("verifying_snapshot")
    snapshot, manifest, _ = prepare_contents(ctx, run)
    with github_for(ctx.job.workspace_id, "recovery", budget=ctx.budget) as (gh, connection):
        destination = destination_preflight(gh, connection, run, manifest, ctx.settings)
    plan = {"snapshot_id": str(snapshot.id), "manifest_sha256": manifest["manifest_sha256"], "destination": destination,
            "target_owner": run.target_owner, "target_name": run.target_name, "private": True,
            "refs": manifest["git"]["refs"], "default_branch": manifest["git"]["default_branch"], "lfs_count": len(manifest.get("lfs", [])),
            "archived_only": [c for c in ARCHIVED_ONLY if manifest["coverage"].get(c, {}).get("status") == "complete"],
            "coverage": manifest["coverage"], "actions_remain_disabled": True, "collection_start": manifest["collection_start"], "collection_end": manifest["collection_end"], "limitations": manifest["limitations"]}
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    ctx.fence()
    with session_scope() as session:
        require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        current = scoped(session, Recovery, run.id, ctx.job.workspace_id)
        current.verified_plan = plan
        current.status = "ready"
    finish(ctx, {"recovery_id": str(run.id), "plan_ready": True})


def run_recovery_apply(ctx):
    with session_scope() as session:
        run = scoped(session, Recovery, uuid.UUID(ctx.job.payload["recovery_id"]), ctx.job.workspace_id)
    if not run.approved_at or not run.verified_plan:
        raise BackItUpError("confirmation_required", "Review and confirm a recovery plan first")
    if run.status == "succeeded":
        finish(ctx, run.report)
        return
    ctx.progress("verifying_snapshot")
    snapshot, manifest, repo = prepare_contents(ctx, run)
    if manifest["manifest_sha256"] != run.verified_plan["manifest_sha256"]:
        raise BackItUpError("plan_changed", "The snapshot changed after plan approval")
    checkpoint = run.checkpoint
    with github_for(ctx.job.workspace_id, "recovery", budget=ctx.budget) as (gh, connection):
        destination = destination_preflight(gh, connection, run, manifest, ctx.settings, allow_existing=bool(checkpoint.get("repository_id")))
        existing = gh.get(f"/repos/{run.target_owner}/{run.target_name}", allow_not_found=True)
        if checkpoint.get("repository_id"):
            if not existing or existing["id"] != checkpoint["repository_id"] or not existing["private"]:
                raise BackItUpError("destination_changed", "The recovery repository was removed, replaced, or made public")
            created = existing
        else:
            ctx.progress("creating_repository")
            ctx.fence()
            path = "/user/repos" if destination["owner"]["type"] != "Organization" else f"/orgs/{run.target_owner}/repos"
            created = gh.request("POST", path, json_body={"name": run.target_name, "private": True, "auto_init": False, "has_issues": True}).json()
            if created["owner"]["id"] != run.target_account_id or not created.get("private"):
                raise BackItUpError("destination_identity", "GitHub created an unexpected repository; recovery stopped")
            ctx.checkpoint(run.id, repository_id=created["id"], phase="created", refs={})
        base = f"/repos/{run.target_owner}/{run.target_name}"
        ctx.progress("disabling_actions")
        gh.request("PUT", base + "/actions/permissions", json_body={"enabled": False})
        if gh.get(base + "/actions/permissions").get("enabled") is not False:
            raise BackItUpError("actions_enabled", "Could not verify that Actions is disabled; no Git data was pushed")

    cached = {}
    def target_token():
        if cached and cached["expires_at"] > time.time() + 180:
            return cached["access_token"]
        ctx.fence()
        connection, token = load_connection(ctx.job.workspace_id, "recovery")
        if connection.id != run.destination_connection_id or connection.provider_user_id != run.destination_user_id:
            raise BackItUpError("destination_identity", "The destination account changed")
        cached.update(access_token=token["access_token"], expires_at=token.get("expires_at", time.time() + 3600))
        return cached["access_token"]

    git = Git(ctx.budget, target_token)
    target = f"https://github.com/{run.target_owner}/{run.target_name}.git"
    actual = git.remote_refs(target)
    expected = manifest["git"]["refs"]
    if actual and not (checkpoint.get("phase") in ("pushing", "pushed", "verified") and actual == expected):
        raise BackItUpError("destination_changed", "Destination changed outside this recovery; no refs were overwritten")
    ctx.progress("restoring_lfs")
    git.push_lfs(repo, target, manifest.get("lfs", []))
    ctx.checkpoint(run.id, phase="pushing", expected_refs=expected)
    ctx.progress("restoring_git")
    verification = git.push(repo, target, expected, checkpoint=actual)
    ctx.checkpoint(run.id, phase="pushed", refs=expected)
    with github_for(ctx.job.workspace_id, "recovery", budget=ctx.budget) as (gh, _):
        if gh.get(base)["id"] != created["id"]:
            raise BackItUpError("destination_changed", "Destination repository identity changed")
        default = manifest["git"]["default_branch"]
        if f"refs/heads/{default}" in expected:
            gh.request("PATCH", base, json_body={"default_branch": default})
        final = gh.get(base)
        if gh.get(base + "/actions/permissions").get("enabled") is not False:
            raise BackItUpError("actions_changed", "Actions was changed externally during recovery")
        if f"refs/heads/{default}" in expected and final.get("default_branch") != default:
            raise BackItUpError("default_branch", "Default branch could not be verified")
    report = {"recovery_id": str(run.id), "snapshot_id": str(snapshot.id), "destination_url": f"https://github.com/{run.target_owner}/{run.target_name}",
              "repository_id": created["id"], "collection_start": manifest["collection_start"], "collection_end": manifest["collection_end"],
              "git": verification, "lfs": {"status": "restored_and_verified" if "lfs" in manifest["selected_categories"] else "skipped", "count": len(manifest.get("lfs", []))},
              "metadata": {c: {"status": "archive_only", "download": f"/api/v1/snapshots/{snapshot.id}/metadata/{c}"} for c in CORE_METADATA if c in manifest.get("metadata", {})},
              "coverage": manifest["coverage"], "limitations": manifest["limitations"], "actions_enabled": False, "resumable": True, "failures": []}
    ctx.fence(committing=True)
    with session_scope() as session:
        require_lease(session, ctx.job.id, ctx.job.worker_id, ctx.job.lease_version)
        current = scoped(session, Recovery, run.id, ctx.job.workspace_id)
        current.status = "succeeded"
        current.report = report
        current.checkpoint = {**current.checkpoint, "phase": "verified", "refs": expected}
    finish(ctx, report)


def download_metadata_file(workspace_id, snapshot_id, category):
    if category not in (*CORE_METADATA, "coverage"):
        raise BackItUpError("not_found", "Unknown metadata category", status=404)
    settings = get_settings()
    signer = ManifestSigner(settings.signing_key_file, settings.verification_keys_dir)
    _, storage, manifest = load_snapshot(workspace_id, snapshot_id, signer)
    item = manifest.get("metadata", {}).get(category)
    if not item:
        raise BackItUpError("not_found", "This category was not archived", status=404)
    raw = storage.get_bytes(item["path"], max_size=50_000_000)
    if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise BackItUpError("metadata_digest", "Metadata archive failed integrity verification")
    return raw


def run_job(job):
    settings = get_settings()
    settings.scratch_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="job-", dir=settings.scratch_dir) as directory:
        budget = Budget(Path(directory), max_bytes=settings.scratch_limit_bytes, deadline=time.monotonic() + settings.job_timeout_seconds)
        ctx = Context(job, budget)
        stop = threading.Event()

        def keep_alive():
            while not stop.wait(max(1, settings.lease_seconds // 3)):
                try:
                    with session_scope() as session:
                        cancelled = heartbeat(session, job.id, job.worker_id, job.lease_version, settings, {"stage": ctx.stage, "detail": ctx.detail})
                    if cancelled:
                        budget.cancelled.set()
                        return
                except Exception:
                    budget.cancelled.set()
                    return

        pulse = threading.Thread(target=keep_alive, daemon=True)
        pulse.start()
        try:
            {"backup": run_backup, "recovery_plan": run_recovery_plan, "recovery_apply": run_recovery_apply}[job.kind](ctx)
        except Exception as exc:
            if isinstance(exc, BackItUpError):
                error = exc
            elif isinstance(exc, (BotoCoreError, ClientError, httpx.HTTPError)):
                error = BackItUpError("provider_error", "Storage or GitHub could not complete the operation. Check credentials, permissions, and connectivity.", transient=not isinstance(exc, ClientError) or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500) >= 500)
            else:
                logger.error("Job %s failed: %s", job.id, type(exc).__name__)
                error = BackItUpError("worker_error", "Backup service could not complete the job; check the service configuration")
            try:
                with session_scope() as session:
                    current = require_lease(session, job.id, job.worker_id, job.lease_version)
                    fail_job(session, current, error, settings)
                    if hasattr(exc, "coverage"):
                        current.result = {"coverage": exc.coverage}
                    if job.kind.startswith("recovery"):
                        recovery = scoped(session, Recovery, uuid.UUID(job.payload["recovery_id"]), job.workspace_id)
                        recovery.status = "failed"
                        recovery.report = {**recovery.report, "failures": [{"code": error.code, "message": error.message}], "resumable": bool(recovery.checkpoint), "recovery_id": str(recovery.id)}
            except BackItUpError:
                pass  # Fenced workers must not overwrite a newer attempt's outcome.
        finally:
            stop.set()
            pulse.join(timeout=5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    settings.validate_production()
    name = os.environ.get("BACKITUP_WORKER_NAME", "worker-1")
    with session_scope() as session:
        session.execute(insert(Worker).values(name=name).on_conflict_do_nothing(index_elements=[Worker.name]))
        worker_id = session.scalar(select(Worker.id).where(Worker.name == name))
    while True:
        try:
            with session_scope() as session:
                worker = session.get(Worker, worker_id)
                job = lease_job(session, worker, settings)
            if job:
                run_job(job)
            else:
                time.sleep(2)
        except BackItUpError as exc:
            logger.error("Worker: %s", exc.code)
            if exc.code == "worker_revoked":
                return
            time.sleep(5)
        except Exception as exc:
            logger.error("Worker unavailable: %s", type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
