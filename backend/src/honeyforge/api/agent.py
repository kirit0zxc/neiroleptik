"""Отдельная граница агента без пользовательских cookies и произвольных target IDs."""

from fastapi import APIRouter

from honeyforge.api.dependencies import Agent, Configuration, Database
from honeyforge.api.schemas import AgentConfigRead, EnrollInput, EnrollRead, HostRead
from honeyforge.control.deployments import DeploymentService
from honeyforge.control.registry import RegistryService

router = APIRouter(prefix="/agent/v1", tags=["agent"])


@router.post("/enroll", response_model=EnrollRead, status_code=201)
def enroll(payload: EnrollInput, db: Database, settings: Configuration) -> EnrollRead:
    return RegistryService(db, settings).enroll(payload)


@router.post("/heartbeat", response_model=HostRead)
def heartbeat(agent: Agent, db: Database, settings: Configuration) -> HostRead:
    return RegistryService(db, settings).heartbeat(agent)


@router.get("/config", response_model=AgentConfigRead)
def config(agent: Agent, db: Database, settings: Configuration) -> AgentConfigRead:
    return DeploymentService(db, settings).config(agent)
