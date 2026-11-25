"""HTTP surface for the gate (compendium section 23).

A thin adapter: authentication, authorization and every governance rule
live in GovernanceService. Run locally with:

    AIDLC_G_DB=gate.sqlite uvicorn --factory aidlc_g.api:build_default_app
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from . import __version__
from .contracts import ContractType
from .errors import GovernanceError
from .service import GovernanceService

_bearer = HTTPBearer(auto_error=False)


def _token(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Optional[str]:
    return creds.credentials if creds else None


class PrincipalIn(BaseModel):
    principal_id: str
    display_name: str
    kind: str
    roles: List[str]


class RunnerIn(BaseModel):
    runner_id: str
    public_key: str


class ContractIn(BaseModel):
    artifact_id: str
    body: Dict[str, Any]


class ApprovalIn(BaseModel):
    version: int
    digest: str
    decision: str = "APPROVE"
    as_role: Optional[str] = None
    rationale: Optional[str] = None


class BuildIn(BaseModel):
    build_id: str
    kind: str = "CANDIDATE"
    source_revision: str
    build_digest: str
    manifest: Dict[str, Any]


class AttestationIn(BaseModel):
    payload: Dict[str, Any]
    signature: str


class ReviewIn(BaseModel):
    review_id: str
    build_id: str
    decision: str
    findings: List[Dict[str, Any]] = []


class WorkItemIn(BaseModel):
    work_item_id: str


class TransitionIn(BaseModel):
    work_item_id: str
    target_stage: str
    refs: Dict[str, Any]
    policy_version: str
    idempotency_key: str


def create_app(service: GovernanceService) -> FastAPI:
    app = FastAPI(title="AIDLC-G Evidence Gate", version=__version__)

    @app.exception_handler(GovernanceError)
    async def _governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.post("/principals", status_code=201)
    def register_principal(body: PrincipalIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.register_principal(token, body.principal_id, body.display_name, body.kind, body.roles)

    @app.post("/runners", status_code=201)
    def register_runner(body: RunnerIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.register_runner(token, body.runner_id, body.public_key)

    @app.post("/contracts/{contract_type}", status_code=201)
    def submit_contract(contract_type: str, body: ContractIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.submit_contract(token, contract_type, body.artifact_id, body.body)

    @app.post("/requirements", status_code=201)
    def submit_requirement(body: ContractIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.submit_contract(token, ContractType.C1.value, body.artifact_id, body.body)

    @app.post("/acceptance/manifest", status_code=201)
    def submit_manifest(body: ContractIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.submit_contract(token, ContractType.C3.value, body.artifact_id, body.body)

    def _approve(artifact_id: str, body: ApprovalIn, token: Optional[str]) -> Dict[str, Any]:
        return service.approve(token, artifact_id, body.version, body.digest, body.decision, body.as_role, body.rationale)

    @app.post("/contracts/{artifact_id}/approve")
    def approve_contract(artifact_id: str, body: ApprovalIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return _approve(artifact_id, body, token)

    @app.post("/requirements/{artifact_id}/approve")
    def approve_requirement(artifact_id: str, body: ApprovalIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return _approve(artifact_id, body, token)

    @app.post("/technical/{artifact_id}/approve")
    def approve_technical(artifact_id: str, body: ApprovalIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return _approve(artifact_id, body, token)

    @app.get("/contracts/{artifact_id}")
    def get_contract(artifact_id: str, version: Optional[int] = None, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.get_artifact(token, artifact_id, version)

    @app.post("/builds/register", status_code=201)
    def register_build(body: BuildIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.register_build(token, body.build_id, body.kind, body.source_revision, body.build_digest, body.manifest)

    @app.post("/evidence/ingest", status_code=201)
    def ingest(body: AttestationIn) -> Dict[str, Any]:
        # Authenticated by the runner signature, not a bearer token.
        return service.ingest_attestation(body.payload, body.signature)

    @app.post("/reviews", status_code=201)
    def review(body: ReviewIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.record_review(token, body.review_id, body.build_id, body.decision, body.findings)

    @app.post("/work-items", status_code=201)
    def open_work_item(body: WorkItemIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.open_work_item(token, body.work_item_id)

    @app.get("/work-items/{work_item_id}")
    def get_work_item(work_item_id: str, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.get_work_item(token, work_item_id)

    @app.get("/work-items/{work_item_id}/reverify")
    def reverify(work_item_id: str, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.reverify(token, work_item_id)

    @app.get("/work-items/{work_item_id}/release-manifest")
    def release_manifest(work_item_id: str, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.release_manifest(token, work_item_id)

    @app.post("/transitions/evaluate")
    def evaluate(body: TransitionIn, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.evaluate_transition(
            token, body.work_item_id, body.target_stage, body.refs, body.policy_version, body.idempotency_key
        )

    @app.get("/audit/verify")
    def verify_audit(token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        service.audit_log(token)  # authenticates the caller
        return service.verify_audit_chain()

    @app.get("/audit/{transition_id}")
    def audit(transition_id: str, token: Optional[str] = Depends(_token)) -> Dict[str, Any]:
        return service.get_transition(token, transition_id)

    return app


def build_default_app() -> FastAPI:
    """uvicorn factory. Prints the bootstrap admin token once for a new store."""
    db_path = os.environ.get("AIDLC_G_DB", "aidlc_g_gate.sqlite")
    try:
        service, admin_token = GovernanceService.bootstrap(db_path)
        print(f"[aidlc-g] new store {db_path}; admin token (shown once): {admin_token}")
    except GovernanceError:
        service = GovernanceService(db_path)
    return create_app(service)
