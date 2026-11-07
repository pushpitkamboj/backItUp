import uuid

import pytest
from backitup.api import app
from backitup.config import Settings
from backitup.errors import BackItUpError
from backitup.schemas import PlanInput
from backitup.storage import ManagedR2Config
from pydantic import ValidationError


def test_backup_plan_does_not_accept_a_client_storage_destination() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PlanInput(
            repository_id=uuid.uuid4(),
            storage_id=uuid.uuid4(),
            retention_accepted=True,
        )


def test_public_api_has_no_customer_storage_mutation_routes() -> None:
    routes = {route.path: route.methods for route in app.routes}
    assert routes["/api/v1/storage"] == {"GET"}
    assert "/api/v1/storage/{storage_id}/verify" not in routes


def test_managed_storage_requires_complete_operator_configuration() -> None:
    assert not Settings().managed_storage_ready()
    assert Settings(
        managed_r2_account_id="0" * 32,
        managed_r2_bucket="backitup-snapshots",
        managed_r2_access_key_id="access",
        managed_r2_secret_access_key="secret",
    ).managed_storage_ready()


def test_managed_r2_does_not_accept_an_invalid_service_account() -> None:
    with pytest.raises(BackItUpError, match="managed R2 account"):
        ManagedR2Config("backitup-snapshots", "workspaces/abc", "not-an-account").endpoint()
