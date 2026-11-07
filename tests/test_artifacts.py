import hashlib
import uuid
from pathlib import Path

import boto3
import pytest
from backitup.artifacts import publish_snapshot, verify_manifest
from backitup.engine import Budget
from backitup.errors import BackItUpError
from backitup.security import ManifestSigner
from backitup.storage import ManagedR2Config, R2Storage
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from moto import mock_aws


@mock_aws
def test_completion_marker_and_signature_guard_snapshot(tmp_path: Path) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="backitup-test-bucket")
    storage = R2Storage(ManagedR2Config("backitup-test-bucket", "backitup", "0" * 32), client=client)
    archive = tmp_path / "repo.tar.zst"
    archive.write_bytes(b"backup bytes")
    workspace_id, repository_id, job_id, snapshot_id = (str(uuid.uuid4()) for _ in range(4))
    prefix = f"snapshots/{repository_id}/{job_id}"
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "workspace_id": workspace_id,
        "repository_id": repository_id,
        "job_id": job_id,
        "git": {"refs": {"refs/heads/main": "a" * 40}},
        "selected_categories": ["git"],
        "coverage": {"git": {"status": "complete"}},
        "archive": {"path": f"{prefix}/repo.tar.zst", "size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        "metadata": {},
    }
    private = Ed25519PrivateKey.generate()
    key_file = tmp_path / "manifest.pem"
    key_file.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    signer = ManifestSigner(key_file)

    saved, size = publish_snapshot(storage, prefix, archive, manifest, {}, signer, Budget(tmp_path, max_bytes=10_000_000))
    assert saved["manifest_sha256"]
    assert size > archive.stat().st_size
    assert storage.exists(f"{prefix}/complete.json")

    storage.put_bytes(f"{prefix}/manifest.json", b"{}")
    with pytest.raises(BackItUpError, match="completion record"):
        verify_manifest(storage, prefix, signer, workspace_id=workspace_id, repository_id=repository_id, job_id=job_id)
