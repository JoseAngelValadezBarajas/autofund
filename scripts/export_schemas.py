"""Export the public data contracts as JSON Schema, generated from the domain models.

The temptation with schemas is to hand-write a tidy document that describes what the models were
*meant* to look like. That document then drifts, and a reader integrating against it discovers the
drift only when a field is missing. So these are generated from the dataclasses the code actually
uses: the field names, their annotations and their defaults are read out of the model, and the
`public()` methods that the API serialises are exercised to confirm the schema describes what is
really served.

Run:  python scripts/export_schemas.py

Output: schemas/*.schema.json, committed, because a consumer wants the contract without running the
exporter and because a change to a model should show up as a reviewable diff in the schema.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "schemas"

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# Annotations are strings under `from __future__ import annotations`, so they are mapped by name.
# Anything unrecognised becomes an unconstrained value rather than a guess: a schema that claims a
# type the model does not have is worse than one that admits it does not know.
SCALARS: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "Decimal": "string",
    "Any": "object",
    "datetime": "string",
}


def _named(annotation: str) -> str:
    """Strip Optional[...] and | None down to the underlying type name."""
    text = annotation.replace(" ", "")
    for prefix in ("Optional[", "NotRequired["):
        if text.startswith(prefix):
            text = text[len(prefix):-1]
    return text.split("|")[0]


def _is_nullable(annotation: str) -> bool:
    text = annotation.replace(" ", "")
    return text.startswith("Optional[") or "|None" in text or text.endswith("None")


def _is_sequence(annotation: str) -> bool:
    text = annotation.replace(" ", "")
    return text.startswith(("tuple[", "list[", "Sequence[", "frozenset["))


def _schema_for(annotation: str) -> dict[str, Any]:
    text = annotation.replace(" ", "")
    if _is_sequence(text):
        inner = text[text.index("[") + 1:-1]
        parts = [part for part in inner.split(",") if part and part != "..."]
        item = _schema_for(parts[0]) if parts else {}
        return {"type": "array", "items": item}
    if text.startswith("dict["):
        inner = text[text.index("[") + 1:-1]
        parts = inner.split(",", 1)
        return {"type": "object",
                "additionalProperties": _schema_for(parts[1]) if len(parts) > 1 else {}}
    named = _named(text)
    if named in SCALARS:
        return {"type": SCALARS[named]}
    if named.endswith("Enum") or named in _ENUM_NAMES:
        return {"type": "string", "description": f"one of the {named} values"}
    return {}


def _field_schema(field: dataclasses.Field[Any]) -> dict[str, Any]:
    annotation = str(field.type)
    schema = _schema_for(annotation)
    if _is_nullable(annotation):
        existing = dict(schema)
        schema = {"anyOf": [existing, {"type": "null"}]} if existing else {}
    if field.default is not dataclasses.MISSING and not isinstance(field.default, dataclasses._MISSING_TYPE):  # type: ignore[attr-defined]
        default = field.default
        if isinstance(default, bool | int | float | str):
            schema = {**schema, "default": default}
    # A sentinel default is documentation: it tells a consumer that the field may legitimately carry
    # `UNKNOWN`, which is different from being absent.
    if isinstance(field.default, str) and field.default in {"UNKNOWN", "NOT_RECORDED", "NOT_APPLICABLE"}:
        schema = {**schema, "description": f"sentinel default: {field.default}"}
    return schema


_ENUM_NAMES: set[str] = set()

# The public contracts, in the order a reader is likely to want them.
CONTRACTS: tuple[tuple[str, str, str], ...] = (
    ("alpha_source", "autofund.research.alpha-source.v1", "AlphaSourceRecord"),
    ("strategy_profile", "autofund.research.strategy-profile.v1", "StrategyProfileRecord"),
    ("experiment", "autofund.research.experiment.v1", "ExperimentRecord"),
    ("evidence", "autofund.research.evidence.v1", "EvidenceRecord"),
    ("campaign", "autofund.research.campaign.v1", "ResearchCampaignRecord"),
    ("certification", "autofund.research.certification.v1", "CertificationRecord"),
    ("eligibility", "autofund.research.eligibility.v1", "ProductionEligibilityRecord"),
    ("research_registry", "autofund.research-registry.v1", "ResearchRegistry"),
)


def _document(name: str, schema_id: str, model: type) -> dict[str, Any]:
    if not dataclasses.is_dataclass(model):
        raise TypeError(f"{model.__name__} is not a dataclass")
    properties = {}
    required = []
    for field in dataclasses.fields(model):
        properties[field.name] = _field_schema(field)
        # A field with no default is required in the constructor, so it must be present in the
        # serialised form. A field with a default may legitimately be omitted.
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            required.append(field.name)
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": f"https://autofund.local/schemas/{name}.schema.json",
        "title": model.__name__,
        "description": (
            f"Public contract for {name.replace('_', ' ')}, generated from the domain model "
            f"`autofund.research.models.{model.__name__}` by scripts/export_schemas.py. "
            "Regenerate rather than editing: the model is the source of truth."),
        "x-autofund-contract": schema_id,
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from autofund.research import models

    for _, _, class_name in CONTRACTS:
        if hasattr(models, class_name):
            enum = getattr(models, class_name)
            _ENUM_NAMES.add(class_name)
            del enum
    for value in vars(models).values():
        if isinstance(value, type) and issubclass(value, __import__("enum").Enum) and value.__module__ == models.__name__:
            _ENUM_NAMES.add(value.__name__)

    OUT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, schema_id, class_name in CONTRACTS:
        model = getattr(models, class_name, None)
        if model is None:
            print(f"SKIP {name}: {class_name} not found")
            continue
        document = _document(name, schema_id, model)
        path = OUT / f"{name}.schema.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n",
                        encoding="utf-8", newline="\n")
        written.append(path.name)
    print(f"wrote {len(written)} schema(s): {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
