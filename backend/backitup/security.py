import base64
import hashlib
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from backitup.errors import BackItUpError


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class CredentialCipher:
    def __init__(self, key_file: Path):
        if not key_file.is_file():
            raise BackItUpError("server_setup", "Server credential protection is not configured", status=503)
        self.cipher = Fernet(key_file.read_bytes().strip())

    def encrypt(self, value: dict) -> str:
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value: str) -> dict:
        return json.loads(self.cipher.decrypt(value.encode()))


class ManifestSigner:
    def __init__(self, private_key_file: Path, verification_dir: Path | None = None):
        self.key: Ed25519PrivateKey | None = None
        self.trusted: dict[str, Ed25519PublicKey] = {}
        if private_key_file.is_file():
            key = serialization.load_pem_private_key(private_key_file.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("Manifest signing requires an Ed25519 key")
            self.key = key
            self.key_id = self.add_trusted(key.public_key())
        if verification_dir and verification_dir.is_dir():
            for path in verification_dir.glob("*.pem"):
                public = serialization.load_pem_public_key(path.read_bytes())
                if not isinstance(public, Ed25519PublicKey):
                    raise ValueError("Invalid manifest verification key")
                self.add_trusted(public)

    def add_trusted(self, public: Ed25519PublicKey) -> str:
        raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        key_id = hashlib.sha256(raw).hexdigest()[:24]
        self.trusted[key_id] = public
        return key_id

    def sign(self, data: bytes) -> bytes:
        if self.key is None:
            raise BackItUpError("server_setup", "Manifest signing is not configured", status=503)
        return json.dumps({"key_id": self.key_id, "signature": base64.b64encode(self.key.sign(data)).decode()}).encode()

    def verify(self, data: bytes, envelope: bytes) -> None:
        try:
            signature = json.loads(envelope)
            key = self.trusted[signature["key_id"]]
            key.verify(base64.b64decode(signature["signature"], validate=True), data)
        except Exception as exc:
            raise BackItUpError("invalid_signature", "Snapshot manifest signature is not trusted") from exc
