import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from backitup.engine import Budget, safe_relative
from backitup.errors import BackItUpError


@dataclass(frozen=True)
class ManagedR2Config:
    bucket: str
    prefix: str
    account_id: str

    def endpoint(self) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise BackItUpError("invalid_bucket", "The managed backup bucket configuration is invalid")
        safe_relative(self.prefix)
        if not re.fullmatch(r"[a-f0-9]{32}", self.account_id):
            raise BackItUpError("invalid_account", "The managed R2 account configuration is invalid")
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


class R2Storage:
    def __init__(self, config: ManagedR2Config, credentials: dict | None = None, *, client=None):
        self.config = config
        if client is not None:  # Explicit dependency injection for local integration tests only.
            self.client = client
            return
        endpoint = config.endpoint()
        credentials = credentials or {}
        if not credentials.get("access_key_id") or not credentials.get("secret_access_key"):
            raise BackItUpError("storage_credentials", "Managed backup storage is not configured", status=503)
        self.client = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                                  aws_access_key_id=credentials["access_key_id"], aws_secret_access_key=credentials["secret_access_key"],
                                  aws_session_token=credentials.get("session_token"),
                                  config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=60,
                                                retries={"max_attempts": 3, "mode": "standard"},
                                                request_checksum_calculation="when_required", response_checksum_validation="when_required"))

    def key(self, relative: str) -> str:
        safe_relative(relative)
        safe_relative(self.config.prefix)
        return f"{self.config.prefix}/{relative}"

    def put_bytes(self, relative: str, value: bytes, *, content_type: str = "application/json") -> None:
        self.client.put_object(Bucket=self.config.bucket, Key=self.key(relative), Body=value, ContentType=content_type)

    def get_bytes(self, relative: str, *, max_size: int = 8_000_000) -> bytes:
        response = self.client.get_object(Bucket=self.config.bucket, Key=self.key(relative))
        body = response["Body"]
        try:
            if response["ContentLength"] > max_size:
                raise BackItUpError("object_limit", "Snapshot metadata exceeds its size limit")
            result = body.read(max_size + 1)
            if len(result) > max_size:
                raise BackItUpError("object_limit", "Snapshot metadata exceeds its size limit")
            return result
        finally:
            body.close()

    def exists(self, relative: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=self.key(relative))
            return True
        except ClientError as exc:
            if str(exc.response["Error"]["Code"]) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def upload(self, relative: str, source: Path, budget: Budget) -> None:
        self.client.upload_file(str(source), self.config.bucket, self.key(relative),
                                Config=TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024, use_threads=False),
                                Callback=lambda _: budget.check())

    def download(self, relative: str, target: Path, budget: Budget, *, expected_size: int, expected_sha256: str) -> None:
        if expected_size < 0 or expected_size > budget.max_bytes:
            raise BackItUpError("disk_limit", "Snapshot exceeds the temporary storage limit")
        result = self.client.get_object(Bucket=self.config.bucket, Key=self.key(relative))
        body = result["Body"]
        digest = hashlib.sha256()
        received = 0
        try:
            if result["ContentLength"] != expected_size:
                raise BackItUpError("archive_size", "Snapshot archive size differs from its manifest")
            with target.open("wb") as output:
                while chunk := body.read(1024 * 1024):
                    budget.check()
                    received += len(chunk)
                    if received > expected_size:
                        raise BackItUpError("archive_size", "Snapshot archive exceeds its expected size")
                    output.write(chunk)
                    digest.update(chunk)
            if received != expected_size or digest.hexdigest() != expected_sha256:
                raise BackItUpError("archive_digest", "Snapshot archive failed integrity verification")
        finally:
            body.close()

    def verify_object(self, relative: str, budget: Budget, size: int, sha256: str) -> None:
        result = self.client.get_object(Bucket=self.config.bucket, Key=self.key(relative))
        digest = hashlib.sha256()
        count = 0
        body = result["Body"]
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                budget.check()
                count += len(chunk)
                if count > size:
                    raise BackItUpError("archive_size", "Uploaded object exceeds its expected size")
                digest.update(chunk)
            if count != size or digest.hexdigest() != sha256:
                raise BackItUpError("archive_digest", "Uploaded object did not match its digest")
        finally:
            body.close()

    def download_url(self, relative: str, filename: str) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.config.bucket, "Key": self.key(relative),
                                                   "ResponseContentDisposition": f'attachment; filename="{filename}"'}, ExpiresIn=60)

    def list(self, relative: str) -> Iterator[dict]:
        prefix = self.key(relative) + "/"
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.config.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                if not item["Key"].startswith(self.config.prefix + "/"):
                    raise BackItUpError("unsafe_path", "Storage returned an object outside its configured prefix")
                yield {"path": item["Key"][len(self.config.prefix) + 1:], "size": item["Size"]}

    def delete_prefix(self, relative: str) -> None:
        # Complete marker disappears before artifact deletion, including interrupted pruning.
        marker = relative + "/complete.json"
        self.client.delete_object(Bucket=self.config.bucket, Key=self.key(marker))
        for item in self.list(relative):
            self.client.delete_object(Bucket=self.config.bucket, Key=self.key(item["path"]))

    def probe(self, probe_id: str) -> dict:
        path = f"probes/{probe_id}/connectivity.json"
        payload = json.dumps({"probe": probe_id}).encode()
        try:
            self.put_bytes(path, payload)
            if self.get_bytes(path) != payload:
                raise BackItUpError("storage_integrity", "Storage connectivity verification returned different bytes")
            self.client.delete_object(Bucket=self.config.bucket, Key=self.key(path))
            # Retention and crash cleanup require deletion within this prefix.
            list(self.list(f"probes/{probe_id}"))
        except (ClientError, BotoCoreError) as exc:
            raise BackItUpError("storage_access", "Managed backup storage is temporarily unavailable.") from exc
        return {"read": True, "write": True, "list": True, "cleanup": True}
