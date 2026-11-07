from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backitup.github import ALL_CATEGORIES, CORE_METADATA, valid_owner, valid_repo


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectRepository(Input):
    installation_id: int = Field(gt=0)
    provider_id: int = Field(gt=0)


class PlanInput(Input):
    repository_id: UUID
    interval_hours: Literal[0, 6, 24] = 24
    retention_days: int = Field(default=30, ge=1, le=3650)
    retention_accepted: Literal[True]
    categories: list[str] = Field(default_factory=lambda: ["git", *CORE_METADATA], max_length=20)
    enabled: bool = True

    @field_validator("categories")
    @classmethod
    def categories_known(cls, value):
        if "git" not in value or not set(value).issubset(ALL_CATEGORIES):
            raise ValueError("Select Git and only supported backup categories")
        return list(dict.fromkeys(value))


class RecoveryInput(Input):
    snapshot_id: UUID
    target_owner: str = Field(min_length=1, max_length=100)
    target_name: str = Field(min_length=1, max_length=100)

    @field_validator("target_owner")
    @classmethod
    def owner_valid(cls, value):
        return valid_owner(value)

    @field_validator("target_name")
    @classmethod
    def name_valid(cls, value):
        return valid_repo(value)


class ApplyInput(Input):
    confirm: Literal[True]
    plan_digest: str = Field(min_length=64, max_length=64)


class HeartbeatInput(Input):
    worker_id: UUID
    lease_version: int = Field(ge=1)
    stage: str = Field(default="running", max_length=80)


class WorkerInput(Input):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
