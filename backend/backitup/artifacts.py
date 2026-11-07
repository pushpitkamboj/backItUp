import hashlib
import json
from pathlib import Path

from backitup.engine import Budget, safe_relative
from backitup.errors import BackItUpError
from backitup.security import ManifestSigner
from backitup.storage import R2Storage

FORMAT_VERSION = 1


def verify_manifest(storage: R2Storage, prefix: str, signer: ManifestSigner, *, workspace_id: str, repository_id: str,
                    job_id: str | None = None, expected_digest: str | None = None) -> tuple[dict, int]:
    safe_relative(prefix)
    marker_bytes = storage.get_bytes(prefix + "/complete.json", max_size=16384)
    marker = json.loads(marker_bytes)
    raw = storage.get_bytes(prefix + "/manifest.json")
    envelope = storage.get_bytes(prefix + "/manifest.sig", max_size=16384)
    digest = hashlib.sha256(raw).hexdigest()
    if marker.get("manifest_sha256") != digest or (expected_digest and digest != expected_digest):
        raise BackItUpError("manifest_digest", "Snapshot manifest differs from its completion record")
    signer.verify(raw, envelope)
    manifest = json.loads(raw)
    if manifest.get("schema_version") != FORMAT_VERSION:
        raise BackItUpError("snapshot_version", "This snapshot format version is not supported")
    if manifest.get("workspace_id") != workspace_id or manifest.get("repository_id") != repository_id or (job_id and manifest.get("job_id") != job_id):
        raise BackItUpError("snapshot_identity", "Snapshot identity does not match the requested backup")
    if not isinstance(manifest.get("git", {}).get("refs"), dict) or not isinstance(manifest.get("coverage"), dict):
        raise BackItUpError("snapshot_format", "Snapshot inventory is invalid")
    for ref, oid in manifest["git"]["refs"].items():
        if not ref.startswith(("refs/heads/", "refs/tags/")) or not isinstance(oid, str) or len(oid) not in (40, 64) or any(c not in "0123456789abcdef" for c in oid):
            raise BackItUpError("snapshot_format", "Snapshot contains an invalid Git reference")
    for category in manifest.get("selected_categories", []):
        if manifest["coverage"].get(category, {}).get("status") != "complete":
            raise BackItUpError("snapshot_incomplete", "A required snapshot category is incomplete")
    objects = [manifest["archive"], *manifest.get("metadata", {}).values()]
    for item in objects:
        path = item.get("path", "")
        safe_relative(path)
        if not path.startswith(prefix + "/") or not isinstance(item.get("size"), int) or item["size"] < 0 or len(item.get("sha256", "")) != 64:
            raise BackItUpError("unsafe_path", "Manifest references an invalid object or an object outside this snapshot")
    total = manifest["archive"]["size"] + sum(v["size"] for v in manifest.get("metadata", {}).values()) + len(raw) + len(envelope) + len(marker_bytes)
    manifest["manifest_sha256"] = digest
    return manifest, total


def publish_snapshot(storage: R2Storage, prefix: str, archive: Path, manifest: dict, metadata: dict[str, Path],
                     signer: ManifestSigner, budget: Budget, before_marker=None) -> tuple[dict, int]:
    """The completion record is the last write; all artifacts are read back before it."""
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    signature = signer.sign(raw)
    archive_info = manifest["archive"]
    storage.upload(archive_info["path"], archive, budget)
    storage.verify_object(archive_info["path"], budget, archive_info["size"], archive_info["sha256"])
    for category, path in metadata.items():
        item = manifest["metadata"][category]
        storage.upload(item["path"], path, budget)
        storage.verify_object(item["path"], budget, item["size"], item["sha256"])
    storage.put_bytes(prefix + "/manifest.json", raw)
    storage.put_bytes(prefix + "/manifest.sig", signature, content_type="application/octet-stream")
    if storage.get_bytes(prefix + "/manifest.json") != raw or storage.get_bytes(prefix + "/manifest.sig") != signature:
        raise BackItUpError("manifest_digest", "Uploaded manifest could not be verified")
    signer.verify(raw, signature)
    if before_marker:
        before_marker()
    budget.check()
    marker = json.dumps({"schema_version": FORMAT_VERSION, "snapshot_id": manifest["snapshot_id"], "manifest_sha256": hashlib.sha256(raw).hexdigest()}).encode()
    storage.put_bytes(prefix + "/complete.json", marker)
    return verify_manifest(storage, prefix, signer, workspace_id=manifest["workspace_id"], repository_id=manifest["repository_id"], job_id=manifest["job_id"])
