"""Маршруты оператора; сериализация и политика прав задаются одним контрактом."""

from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from honeyforge.api.command_schemas import CommandAccepted, CommandCreate, JobRead
from honeyforge.api.dependencies import Administrator, Configuration, Database, Operator, Reader
from honeyforge.api.schemas import (
    AuditRead,
    DeploymentCreate,
    DeploymentRead,
    EnrollmentRead,
    HostRead,
    ProfileCreate,
    ProfileRead,
    ProfileUpdate,
    RevisionRead,
)
from honeyforge.control.commands import CommandService
from honeyforge.control.deployments import DeploymentService
from honeyforge.control.profiles import ProfileService
from honeyforge.control.registry import RegistryService
from honeyforge.models import AuditEvent

router = APIRouter(prefix="/api/v1", tags=["control"])


@router.get("/profiles", response_model=list[ProfileRead])
def list_profiles(
    user: Reader,
    db: Database,
    settings: Configuration,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    archived: bool = False,
) -> list[ProfileRead]:
    return ProfileService(db, settings).list_profiles(limit, offset, archived)


@router.post("/profiles", response_model=ProfileRead, status_code=201)
def create_profile(
    payload: ProfileCreate, user: Operator, db: Database, settings: Configuration
) -> ProfileRead:
    return ProfileService(db, settings).create(payload, user)


@router.get("/profiles/{profile_id}", response_model=ProfileRead)
def get_profile(
    profile_id: UUID, user: Reader, db: Database, settings: Configuration
) -> ProfileRead:
    service = ProfileService(db, settings)
    return service.read(service.get(profile_id))


@router.patch("/profiles/{profile_id}", response_model=ProfileRead)
def update_profile(
    profile_id: UUID, payload: ProfileUpdate, user: Operator, db: Database, settings: Configuration
) -> ProfileRead:
    return ProfileService(db, settings).update(profile_id, payload, user)


@router.delete("/profiles/{profile_id}", status_code=204)
def archive_profile(
    profile_id: UUID, user: Operator, db: Database, settings: Configuration
) -> None:
    ProfileService(db, settings).archive(profile_id, user)


@router.get("/profiles/{profile_id}/revisions", response_model=list[RevisionRead])
def profile_revisions(
    profile_id: UUID,
    user: Reader,
    db: Database,
    settings: Configuration,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[RevisionRead]:
    return ProfileService(db, settings).revisions(profile_id, limit, offset)


@router.post("/enrollments", response_model=EnrollmentRead, status_code=201)
def issue_enrollment(user: Administrator, db: Database, settings: Configuration) -> EnrollmentRead:
    return RegistryService(db, settings).issue(user)


@router.get("/hosts", response_model=list[HostRead])
def list_hosts(
    user: Reader,
    db: Database,
    settings: Configuration,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[HostRead]:
    return RegistryService(db, settings).list(limit, offset)


@router.get("/hosts/{host_id}", response_model=HostRead)
def get_host(host_id: UUID, user: Reader, db: Database, settings: Configuration) -> HostRead:
    service = RegistryService(db, settings)
    return service.read(service.get(host_id))


@router.post("/hosts/{host_id}/revoke", response_model=HostRead)
def revoke_host(
    host_id: UUID, user: Administrator, db: Database, settings: Configuration
) -> HostRead:
    return RegistryService(db, settings).revoke(host_id, user)


@router.post("/deployments", response_model=DeploymentRead, status_code=201)
def create_deployment(
    payload: DeploymentCreate, user: Operator, db: Database, settings: Configuration
) -> DeploymentRead:
    return DeploymentService(db, settings).create(payload, user)


@router.get("/deployments", response_model=list[DeploymentRead])
def list_deployments(
    user: Reader,
    db: Database,
    settings: Configuration,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[DeploymentRead]:
    return DeploymentService(db, settings).list(limit, offset)


@router.get("/deployments/{deployment_id}", response_model=DeploymentRead)
def get_deployment(
    deployment_id: UUID, user: Reader, db: Database, settings: Configuration
) -> DeploymentRead:
    return DeploymentService(db, settings).get(deployment_id)


@router.post(
    "/deployments/{deployment_id}/commands", response_model=CommandAccepted, status_code=202
)
def create_command(
    deployment_id: UUID,
    payload: CommandCreate,
    user: Operator,
    db: Database,
    settings: Configuration,
) -> CommandAccepted:
    return CommandService(db, settings).create(deployment_id, payload, user)


@router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: UUID, user: Reader, db: Database, settings: Configuration) -> JobRead:
    return CommandService(db, settings).get(job_id)


@router.get("/audit", response_model=list[AuditRead])
def list_audit(
    user: Administrator,
    db: Database,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[AuditRead]:
    rows = db.scalars(
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return [AuditRead.model_validate(item) for item in rows]
