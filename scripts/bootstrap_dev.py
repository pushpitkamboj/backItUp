"""Create non-production server keys for a local BackItUp checkout.

Existing files are deliberately never replaced. Production deployments must use
their secret manager or mounted, access-restricted secret files instead.
"""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def write_once(path: Path, value: bytes) -> bool:
    if path.exists():
        return False
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
    return True


def main() -> None:
    secret_dir = Path("secrets")
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    signing_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    created = [
        write_once(secret_dir / "credentials.key", Fernet.generate_key() + b"\n"),
        write_once(secret_dir / "manifest.pem", signing_key),
        write_once(secret_dir / "internal.token", secrets.token_urlsafe(32).encode() + b"\n"),
    ]
    print(f"Local development secrets ready ({sum(created)} created; existing secrets preserved).")


if __name__ == "__main__":
    main()
