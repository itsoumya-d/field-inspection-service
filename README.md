# Field Inspection Service

Production-oriented backend for property inspection workflows: properties,
inspection templates, inspections, per-item results, photo evidence checksums,
defects, corrective actions, audit history, idempotent inspection starts and
role-based API access.

## Why this exists

Property managers run move-in, move-out and routine inspections on phones,
often with poor connectivity. The difficult parts are not the form UI: they are
duplicate submissions, partial results, photo evidence integrity, defect
tracking, and a reliable audit trail. This service models those workflows
directly and ships with tests and CI.

## Features

- Properties and reusable inspection templates with critical/non-critical items
- Inspections with idempotency keys: safe retries return the same inspection
- Per-item pass/fail, notes, and SHA-256 photo evidence checksums
- Completion gate: every template item must have a result
- Automatic defect creation for failed items (major for critical items, minor otherwise)
- Defect resolution with notes
- Property summary: inspections, completions, defects, open defects
- Append-only audit events
- API-key RBAC: viewer, inspector, admin
- PostgreSQL or SQLite, SQLAlchemy 2, Alembic migrations, Docker, GitHub Actions

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
DATABASE_URL=sqlite:///./inspection.db .venv/bin/alembic upgrade head
API_KEYS='{"dev-admin":"admin","dev-inspector":"inspector","dev-viewer":"viewer"}' \
  .venv/bin/uvicorn inspection.api:app --reload
```

## API sketch

```bash
curl -s -X POST localhost:8000/properties \
  -H 'X-API-Key: dev-admin' -H 'content-type: application/json' \
  -d '{"name":"12 Queen Street","address":"Auckland"}'
```

## Test

```bash
.venv/bin/pytest
```

## Honest scope

This is a real, tested backend service, not a production deployment. It has no
mobile client, object storage uploader, billing, or tenancy isolation beyond the
API-key roles. The README does not claim production usage.
