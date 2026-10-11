"""HTTP роли и DTO расследования используют общую cookie/CSRF авторизацию."""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import AwareDatetime, BeforeValidator

from honeyforge.api.dependencies import Operator, Reader
from honeyforge.api.investigation_schemas import (
    AlertPage,
    ArtifactPage,
    ArtifactRead,
    HoneytokenPage,
    LookupInput,
    LookupRead,
    TriggerPage,
)
from honeyforge.common import utcnow
from honeyforge.investigation.service import InvestigationService


def rfc3339(value: object) -> object:
    """HTTP принимает только RFC3339, без числовых epoch и неявного timezone."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("Требуется RFC3339 с timezone")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(rfc3339)]


def create_router(service: InvestigationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["investigation"])

    @router.get("/artifacts", response_model=ArtifactPage)
    def artifacts(
        user: Reader,
        session_id: UUID | None = None,
        deployment_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        return service.artifacts(session_id, deployment_id, cursor, limit)

    @router.get(
        "/artifacts/{artifact_id}/content",
        response_class=Response,
        responses={
            200: {"content": {"application/octet-stream": {}}},
            410: {"description": "ARTIFACT_EXPIRED"},
        },
    )
    def download(artifact_id: UUID, user: Operator) -> Response:
        content = service.download(artifact_id)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="honeyforge-artifact-{artifact_id}.bin"'
                )
            },
        )

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
    def artifact(artifact_id: UUID, user: Reader) -> dict[str, Any]:
        return service.artifact(artifact_id)

    @router.get("/honeytokens", response_model=HoneytokenPage)
    def tokens(
        user: Reader,
        deployment_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        return service.tokens(deployment_id, cursor, limit)

    @router.get("/honeytokens/{token_id}/triggers", response_model=TriggerPage)
    def triggers(
        token_id: UUID,
        user: Reader,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        return service.triggers(token_id, cursor, limit)

    @router.get("/alerts", response_model=AlertPage)
    def alerts(
        user: Reader,
        deployment_id: UUID | None = None,
        state: str | None = None,
        severity: str | None = None,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        return service.alerts(deployment_id, state, severity, cursor, limit)

    @router.post("/events/lookup", response_model=LookupRead)
    def lookup(payload: LookupInput, user: Reader) -> dict[str, Any]:
        return service.lookup([(r.agent_id, r.event_id) for r in payload.refs])

    @router.get(
        "/iocs.csv",
        response_class=Response,
        responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
    )
    def csv(
        user: Reader,
        start: Annotated[Timestamp, Query(alias="from")],
        end: Annotated[Timestamp, Query(alias="to")],
        deployment_id: UUID | None = None,
    ) -> Response:
        content = service.csv(start, end, deployment_id)
        filename = utcnow().strftime("honeyforge-iocs-%Y%m%dT%H%M%SZ.csv")
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
