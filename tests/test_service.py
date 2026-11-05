from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from inspection.api import create_app
from inspection.db import Base
from inspection.service import ConflictError, InspectionService, NotFoundError, ValidationError

API_KEYS = {"admin-key": "admin", "inspector-key": "inspector", "viewer-key": "viewer"}


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    yield db
    db.close()


@pytest.fixture()
def client(session):
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    from fastapi.testclient import TestClient

    return TestClient(create_app(session_factory=factory, api_keys=API_KEYS, init_database=False))


def seed(service: InspectionService):
    prop = service.create_property("12 Queen Street", "Auckland")
    template = service.create_template("Move out", "move_out", [
        {"code": "walls", "label": "Walls", "critical": True},
        {"code": "oven", "label": "Oven", "critical": False},
    ])
    return prop, template


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_property(session):
    service = InspectionService(session)
    row = service.create_property("12 Queen Street", "Auckland")
    assert row.id == 1
    assert row.name == "12 Queen Street"


def test_duplicate_property_rejected(session):
    service = InspectionService(session)
    service.create_property("12 Queen Street")
    with pytest.raises(ValidationError):
        service.create_property("12 Queen Street")


def test_create_template(session):
    service = InspectionService(session)
    _, template = seed(service)
    assert template.inspection_type == "move_out"
    assert len(template.items) == 2


def test_duplicate_template_item_rejected(session):
    service = InspectionService(session)
    with pytest.raises(ValidationError):
        service.create_template("Bad", "move_out", [
            {"code": "walls", "label": "Walls"},
            {"code": "walls", "label": "Walls again"},
        ])


def test_start_inspection(session):
    service = InspectionService(session)
    prop, template = seed(service)
    row = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    assert row.status == "in_progress"
    assert row.idempotency_key == "key-1"


def test_idempotent_start_returns_same_inspection(session):
    service = InspectionService(session)
    prop, template = seed(service)
    first = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    second = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    assert first.id == second.id


def test_idempotency_key_conflict(session):
    service = InspectionService(session)
    prop, template = seed(service)
    other = service.create_property("99 Queen Street")
    service.start_inspection(prop.id, template.id, "Sam", "key-1")
    with pytest.raises(ConflictError):
        service.start_inspection(other.id, template.id, "Sam", "key-1")


def test_complete_requires_all_results(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    service.record_result(inspection.id, "walls", True)
    with pytest.raises(ValidationError):
        service.complete_inspection(inspection.id)


def test_failed_critical_item_creates_major_defect(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    service.record_result(inspection.id, "walls", False, "Hole in wall")
    service.record_result(inspection.id, "oven", True)
    defects = service.complete_inspection(inspection.id)
    assert len(defects) == 1
    assert defects[0].severity == "major"


def test_failed_noncritical_item_creates_minor_defect(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    service.record_result(inspection.id, "walls", True)
    service.record_result(inspection.id, "oven", False, "Oven dirty")
    defects = service.complete_inspection(inspection.id)
    assert defects[0].severity == "minor"


def test_resolve_defect(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    service.record_result(inspection.id, "walls", False, "Hole")
    service.record_result(inspection.id, "oven", True)
    defect = service.complete_inspection(inspection.id)[0]
    resolved = service.resolve_defect(defect.id, "Patched and painted")
    assert resolved.status == "resolved"


def test_summary_counts(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    service.record_result(inspection.id, "walls", False, "Hole")
    service.record_result(inspection.id, "oven", True)
    service.complete_inspection(inspection.id)
    summary = service.summary(prop.id)
    assert summary == {"property_id": prop.id, "inspections": 1, "completed": 1, "defects": 1, "open_defects": 1}


def test_photo_hash_validated(session):
    service = InspectionService(session)
    prop, template = seed(service)
    inspection = service.start_inspection(prop.id, template.id, "Sam", "key-1")
    with pytest.raises(ValidationError):
        service.record_result(inspection.id, "walls", True, photo_sha256="not-a-hash")


def test_audit_trail(session):
    service = InspectionService(session)
    prop, template = seed(service)
    service.start_inspection(prop.id, template.id, "Sam", "key-1")
    actions = [event["action"] for event in service.audit()]
    assert "property_created" in actions
    assert "inspection_started" in actions


def test_api_requires_auth(client):
    assert client.post("/properties", json={"name": "Test"}).status_code == 401


def test_api_viewer_cannot_create(client):
    response = client.post("/properties", json={"name": "Test"}, headers={"X-API-Key": "viewer-key"})
    assert response.status_code == 403


def test_api_workflow(client):
    headers = {"X-API-Key": "admin-key"}
    prop = client.post("/properties", json={"name": "12 Queen Street"}, headers=headers).json()
    template = client.post("/templates", json={
        "name": "Move out",
        "items": [{"code": "walls", "label": "Walls", "critical": True}],
    }, headers=headers).json()
    inspection = client.post("/inspections", json={
        "property_id": prop["id"], "template_id": template["id"], "inspector": "Sam", "idempotency_key": "api-1",
    }, headers={"X-API-Key": "inspector-key"}).json()
    client.post(f"/inspections/{inspection['id']}/results", json={
        "item_code": "walls", "passed": False, "note": "Hole",
    }, headers={"X-API-Key": "inspector-key"})
    completed = client.post(f"/inspections/{inspection['id']}/complete", headers={"X-API-Key": "inspector-key"}).json()
    assert completed["defects"][0]["severity"] == "major"
    summary = client.get(f"/properties/{prop['id']}/summary", headers={"X-API-Key": "viewer-key"}).json()
    assert summary["open_defects"] == 1


def test_missing_inspection(session):
    service = InspectionService(session)
    with pytest.raises(NotFoundError):
        service.complete_inspection(999)
