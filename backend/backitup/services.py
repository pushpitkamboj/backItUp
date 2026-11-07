import uuid
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from backitup.config import Settings, get_settings
from backitup.db import session_scope
from backitup.errors import BackItUpError
from backitup.github import GitHub, refresh_user_token
from backitup.models import Connection, StorageProfile, Workspace
from backitup.security import CredentialCipher
from backitup.storage import ManagedR2Config, R2Storage


def scoped(session, model, identifier, workspace_id):
    value = session.scalar(select(model).where(model.id == identifier, model.workspace_id == workspace_id))
    if value is None:
        raise BackItUpError("not_found", "Resource was not found in this workspace", status=404)
    return value


def cipher(settings: Settings | None = None):
    return CredentialCipher((settings or get_settings()).credential_key_file)


def managed_storage_for(session: Session, workspace: Workspace, settings: Settings | None = None) -> StorageProfile:
    """Get the one internal storage partition assigned to a workspace.

    Storage is a product-managed service, so its credentials and bucket never
    cross the browser boundary. Keeping the profile records the bucket/prefix
    that owns old snapshots if the service moves to a new bucket later.
    """
    settings = settings or get_settings()
    if not settings.managed_storage_ready():
        raise BackItUpError("managed_storage_unavailable", "BackItUp Cloud is not configured yet", status=503)
    profile = session.scalar(select(StorageProfile).where(StorageProfile.workspace_id == workspace.id))
    if profile:
        return profile
    config = ManagedR2Config(settings.managed_r2_bucket, f"workspaces/{workspace.id}", settings.managed_r2_account_id)
    config.endpoint()  # Validate operator configuration before creating a usable plan.
    profile = StorageProfile(workspace_id=workspace.id, name="BackItUp Cloud", bucket=config.bucket,
                             prefix=config.prefix, account_id=config.account_id)
    session.add(profile)
    session.flush()
    return profile


def storage_for(profile: StorageProfile, settings: Settings | None = None) -> R2Storage:
    settings = settings or get_settings()
    if not settings.managed_storage_ready():
        raise BackItUpError("managed_storage_unavailable", "BackItUp Cloud is not configured yet", status=503)
    config = ManagedR2Config(profile.bucket, profile.prefix, profile.account_id)
    credentials = {"access_key_id": settings.managed_r2_access_key_id, "secret_access_key": settings.managed_r2_secret_access_key}
    return R2Storage(config, credentials)


def load_connection(workspace_id: uuid.UUID, purpose: str, settings: Settings | None = None) -> tuple[Connection, dict]:
    settings = settings or get_settings()
    with session_scope() as session:
        connection = session.scalar(select(Connection).where(Connection.workspace_id == workspace_id, Connection.purpose == purpose))
        if not connection:
            raise BackItUpError("github_not_connected", f"Connect the {purpose} GitHub account first", status=409)
        encrypted = connection.encrypted_token
    saved = cipher(settings).decrypt(encrypted)
    client_id = getattr(settings, f"{purpose}_client_id")
    secret = getattr(settings, f"{purpose}_client_secret")
    renewed = refresh_user_token(client_id, secret, saved)
    if renewed != saved:
        with session_scope() as session:
            current = scoped(session, Connection, connection.id, workspace_id)
            if current.encrypted_token == encrypted:
                current.encrypted_token = cipher(settings).encrypt(renewed)
            else:
                renewed = cipher(settings).decrypt(current.encrypted_token)
    return connection, renewed


@contextmanager
def github_for(workspace_id, purpose: str, settings: Settings | None = None, budget=None):
    settings = settings or get_settings()
    connection, credentials = load_connection(workspace_id, purpose, settings)
    github = GitHub(credentials["access_token"], settings.github_api_version, budget=budget)
    try:
        yield github, connection
    finally:
        github.close()


