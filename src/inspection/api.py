from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from .auth import load_api_keys, require_role
from .db import SessionLocal, init_db
from .service import ConflictError, InspectionService, NotFoundError, ValidationError


class PropertyIn(BaseModel):
    name: str
    address: str = ""


class TemplateIn(BaseModel):
    name: str
    inspection_type: str = "move_out"
    items: list[dict[str, Any]]


class InspectionIn(BaseModel):
    property_id: int
    template_id: int
    inspector: str = ""
    idempotency_key: str


class ResultIn(BaseModel):
    item_code: str
    passed: bool
    note: str = ""
    photo_sha256: str = ""


class ResolveIn(BaseModel):
    note: str


def create_app(session_factory: sessionmaker = SessionLocal, api_keys: dict[str, str] | None = None, init_database: bool = True) -> FastAPI:
    if init_database:
        init_db()
    keys = api_keys or load_api_keys()
    app = FastAPI(title="Field inspection service", version="1.0.0")

    def db_session():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    viewer = require_role("viewer", keys)
    inspector = require_role("inspector", keys)
    admin = require_role("admin", keys)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(session: Session = Depends(db_session)) -> dict[str, str]:
        session.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/properties")
    def create_property(payload: PropertyIn, session: Session = Depends(db_session), actor: str = Depends(admin)) -> dict[str, Any]:
        try:
            row = InspectionService(session, actor).create_property(payload.name, payload.address)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": row.id, "name": row.name}

    @app.post("/templates")
    def create_template(payload: TemplateIn, session: Session = Depends(db_session), actor: str = Depends(admin)) -> dict[str, Any]:
        try:
            row = InspectionService(session, actor).create_template(payload.name, payload.inspection_type, payload.items)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": row.id, "name": row.name}

    @app.post("/inspections")
    def start_inspection(payload: InspectionIn, session: Session = Depends(db_session), actor: str = Depends(inspector)) -> dict[str, Any]:
        try:
            row = InspectionService(session, actor).start_inspection(payload.property_id, payload.template_id, payload.inspector, payload.idempotency_key)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": row.id, "status": row.status}

    @app.post("/inspections/{inspection_id}/results")
    def record_result(inspection_id: int, payload: ResultIn, session: Session = Depends(db_session), actor: str = Depends(inspector)) -> dict[str, Any]:
        try:
            row = InspectionService(session, actor).record_result(inspection_id, payload.item_code, payload.passed, payload.note, payload.photo_sha256)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": row.id, "passed": row.passed}

    @app.post("/inspections/{inspection_id}/complete")
    def complete(inspection_id: int, session: Session = Depends(db_session), actor: str = Depends(inspector)) -> dict[str, Any]:
        try:
            defects = InspectionService(session, actor).complete_inspection(inspection_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"defects": [{"id": d.id, "item_code": d.item_code, "severity": d.severity} for d in defects]}

    @app.post("/defects/{defect_id}/resolve")
    def resolve(defect_id: int, payload: ResolveIn, session: Session = Depends(db_session), actor: str = Depends(inspector)) -> dict[str, Any]:
        try:
            row = InspectionService(session, actor).resolve_defect(defect_id, payload.note)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": row.id, "status": row.status}

    @app.get("/properties/{property_id}/summary")
    def summary(property_id: int, session: Session = Depends(db_session), _: str = Depends(viewer)) -> dict[str, Any]:
        return InspectionService(session).summary(property_id)

    @app.get("/audit")
    def audit(session: Session = Depends(db_session), _: str = Depends(admin)) -> dict[str, Any]:
        return {"audit": InspectionService(session).audit()}

    return app


app = create_app()
