"""Отдельная граница агента без пользовательских cookies и произвольных target IDs."""

from uuid import UUID

from fastapi import APIRouter

from honeyforge.api.command_schemas import (
    CommandAck,
    CommandClaim,
    CommandClaimRead,
    JobRead,
    LeaseInput,
    LeaseRead,
)
from honeyforge.api.dependencies import Agent, Configuration, Database
from honeyforge.api.schemas import AgentConfigRead, EnrollInput, EnrollRead, HostRead
from honeyforge.control.commands import CommandService
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


@router.post("/commands/claim", response_model=CommandClaimRead)
def claim_command(
    payload: CommandClaim,
    agent: Agent,
    db: Database,
    settings: Configuration,
) -> CommandClaimRead:
    return CommandService(db, settings).claim(agent, payload.command_id)


@router.post("/commands/{command_id}/renew", response_model=LeaseRead)
def renew_command(
    command_id: UUID,
    payload: LeaseInput,
    agent: Agent,
    db: Database,
    settings: Configuration,
) -> LeaseRead:
    return CommandService(db, settings).renew(command_id, payload, agent)


@router.post("/commands/{command_id}/ack", response_model=JobRead)
def acknowledge_command(
    command_id: UUID,
    payload: CommandAck,
    agent: Agent,
    db: Database,
    settings: Configuration,
) -> JobRead:
    return CommandService(db, settings).ack(command_id, payload, agent)
