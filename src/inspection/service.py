from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .tables import AuditRow, DefectRow, InspectionRow, PropertyRow, ResultRow, TemplateItemRow, TemplateRow


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    pass


class InspectionService:
    def __init__(self, session: Session, actor: str = "system") -> None:
        self.session = session
        self.actor = actor

    def _audit(self, action: str, reference: str, detail: str = "") -> None:
        self.session.add(AuditRow(action=action, actor=self.actor, reference=reference, detail=detail))

    def create_property(self, name: str, address: str = "") -> PropertyRow:
        if not name.strip():
            raise ValidationError("property name is required")
        if self.session.scalar(select(PropertyRow).where(PropertyRow.name == name)):
            raise ValidationError("property already exists")
        row = PropertyRow(name=name, address=address)
        self.session.add(row)
        self.session.flush()
        self._audit("property_created", name)
        self.session.commit()
        return row

    def create_template(self, name: str, inspection_type: str, items: list[dict]) -> TemplateRow:
        if not name.strip() or not items:
            raise ValidationError("template name and items are required")
        if self.session.scalar(select(TemplateRow).where(TemplateRow.name == name)):
            raise ValidationError("template already exists")
        template = TemplateRow(name=name, inspection_type=inspection_type)
        self.session.add(template)
        self.session.flush()
        codes: set[str] = set()
        for item in items:
            code = str(item.get("code") or "").strip()
            label = str(item.get("label") or "").strip()
            if not code or not label:
                raise ValidationError("template item code and label are required")
            if code in codes:
                raise ValidationError(f"duplicate template item: {code}")
            codes.add(code)
            self.session.add(TemplateItemRow(template_id=template.id, code=code, label=label, critical=bool(item.get("critical", False))))
        self._audit("template_created", name, f"items={len(items)}")
        self.session.commit()
        return template

    def start_inspection(self, property_id: int, template_id: int, inspector: str, idempotency_key: str) -> InspectionRow:
        if not idempotency_key.strip():
            raise ValidationError("idempotency_key is required")
        if self.session.get(PropertyRow, property_id) is None:
            raise NotFoundError("property not found")
        if self.session.get(TemplateRow, template_id) is None:
            raise NotFoundError("template not found")
        existing = self.session.scalar(select(InspectionRow).where(InspectionRow.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.property_id == property_id and existing.template_id == template_id:
                return existing
            raise ConflictError("idempotency key reused for a different inspection")
        row = InspectionRow(property_id=property_id, template_id=template_id, inspector=inspector, idempotency_key=idempotency_key)
        self.session.add(row)
        self.session.flush()
        self._audit("inspection_started", str(row.id), f"property={property_id}")
        self.session.commit()
        return row

    def record_result(self, inspection_id: int, item_code: str, passed: bool, note: str = "", photo_sha256: str = "") -> ResultRow:
        inspection = self.session.get(InspectionRow, inspection_id)
        if inspection is None:
            raise NotFoundError("inspection not found")
        if inspection.status != "in_progress":
            raise ValidationError("inspection is already completed")
        item = self.session.scalar(select(TemplateItemRow).where(TemplateItemRow.template_id == inspection.template_id, TemplateItemRow.code == item_code))
        if item is None:
            raise NotFoundError("template item not found")
        if photo_sha256 and (len(photo_sha256) != 64 or any(c not in "0123456789abcdef" for c in photo_sha256.lower())):
            raise ValidationError("photo_sha256 must be 64 hex characters")
        row = self.session.scalar(select(ResultRow).where(ResultRow.inspection_id == inspection_id, ResultRow.item_code == item_code))
        if row is None:
            row = ResultRow(inspection_id=inspection_id, item_code=item_code, passed=passed, note=note, photo_sha256=photo_sha256.lower())
            self.session.add(row)
        else:
            row.passed = passed
            row.note = note
            row.photo_sha256 = photo_sha256.lower()
        self._audit("result_recorded", f"{inspection_id}/{item_code}", f"passed={passed}")
        self.session.commit()
        return row

    def complete_inspection(self, inspection_id: int) -> list[DefectRow]:
        inspection = self.session.get(InspectionRow, inspection_id)
        if inspection is None:
            raise NotFoundError("inspection not found")
        if inspection.status == "completed":
            return list(inspection.defects)
        items = list(self.session.scalars(select(TemplateItemRow).where(TemplateItemRow.template_id == inspection.template_id)).all())
        results = {r.item_code: r for r in inspection.results}
        missing = [item.code for item in items if item.code not in results]
        if missing:
            raise ValidationError(f"inspection is missing results: {missing}")
        defects = []
        for item in items:
            result = results[item.code]
            if not result.passed:
                defects.append(DefectRow(
                    inspection_id=inspection_id,
                    item_code=item.code,
                    description=result.note or f"Failed inspection item: {item.label}",
                    severity="major" if item.critical else "minor",
                ))
        inspection.status = "completed"
        inspection.completed_at = datetime.now(timezone.utc)
        self.session.add_all(defects)
        self._audit("inspection_completed", str(inspection_id), f"defects={len(defects)}")
        self.session.commit()
        return defects

    def resolve_defect(self, defect_id: int, note: str) -> DefectRow:
        defect = self.session.get(DefectRow, defect_id)
        if defect is None:
            raise NotFoundError("defect not found")
        if defect.status == "resolved":
            raise ValidationError("defect already resolved")
        if not note.strip():
            raise ValidationError("resolution note is required")
        defect.status = "resolved"
        defect.description = f"{defect.description}; resolved: {note}"
        self._audit("defect_resolved", str(defect.id), note)
        self.session.commit()
        return defect

    def summary(self, property_id: int) -> dict:
        inspections = list(self.session.scalars(select(InspectionRow).where(InspectionRow.property_id == property_id)).all())
        defects = [d for i in inspections for d in i.defects]
        return {
            "property_id": property_id,
            "inspections": len(inspections),
            "completed": sum(1 for i in inspections if i.status == "completed"),
            "defects": len(defects),
            "open_defects": sum(1 for d in defects if d.status == "open"),
        }

    def audit(self) -> list[dict]:
        rows = self.session.scalars(select(AuditRow).order_by(AuditRow.id)).all()
        return [
            {"at": r.at.isoformat(), "action": r.action, "actor": r.actor, "reference": r.reference, "detail": r.detail}
            for r in rows
        ]
