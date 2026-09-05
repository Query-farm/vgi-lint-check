"""Compose semantic models from several simultaneously attached catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .model import Catalog
from .semantic_model import (
    SemanticDiagnostic,
    SemanticEntity,
    _normalize_relationship,
    build_semantic_model,
)

ResolutionStatus = Literal["resolved", "unresolved", "ambiguous", "conflicted", "unavailable"]
Attestation = Literal["unilateral", "corroborated", "third_party"]


@dataclass(frozen=True)
class FederatedRelationship:
    """One reconciled relationship with independent resolution and trust state."""

    relationship_id: str
    definition: dict[str, Any]
    resolution_status: ResolutionStatus
    attestation: Attestation
    host_aliases: tuple[str, ...]


@dataclass
class FederatedSemanticModel:
    """Resolved semantic graph for a set of runtime attachments."""

    entities: dict[tuple[str, str], list[SemanticEntity]]
    relationships: dict[str, FederatedRelationship]
    diagnostics: list[SemanticDiagnostic]


def build_federated_semantic_model(catalogs: dict[str, Catalog]) -> FederatedSemanticModel:
    """Normalize several catalogs and reconcile their relationship assertions."""
    models = {alias: build_semantic_model(catalog) for alias, catalog in catalogs.items()}
    diagnostics = [diagnostic for model in models.values() for diagnostic in model.diagnostics]
    entities: dict[tuple[str, str], list[SemanticEntity]] = {}
    for model in models.values():
        for key, entity in model.entities.items():
            entities.setdefault(key, []).append(entity)

    declarations: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for alias, model in models.items():
        owner_id = str((model.catalog or {}).get("catalog_id", ""))
        for relationship_id, relationship in model.relationships.items():
            declarations.setdefault(relationship_id, []).append((alias, owner_id, relationship))

    relationships: dict[str, FederatedRelationship] = {}
    attached_catalog_ids = {
        str((model.catalog or {}).get("catalog_id", "")) for model in models.values()
    }
    for relationship_id, assertions in declarations.items():
        normalized = {_jsonable(_normalize_relationship(value)) for _, _, value in assertions}
        definition = assertions[0][2]
        from_ref = definition.get("from", {})
        to_ref = definition.get("to", {})
        endpoint_ids = {str(from_ref.get("catalog_id", "")), str(to_ref.get("catalog_id", ""))}
        endpoint_attesters = endpoint_ids & {owner for _, owner, _ in assertions}
        attestation: Attestation = (
            "corroborated"
            if len(endpoint_attesters) == 2
            else "unilateral"
            if endpoint_attesters
            else "third_party"
        )
        status: ResolutionStatus
        if len(normalized) > 1:
            status = "conflicted"
        else:
            counts: list[int] = []
            for ref in (from_ref, to_ref):
                key = (str(ref.get("catalog_id", "")), str(ref.get("entity_id", "")))
                candidates = entities.get(key, [])
                endpoint_hosts = {alias for alias, owner, _ in assertions if owner == key[0]}
                if endpoint_hosts and any(
                    candidate.host.database in endpoint_hosts for candidate in candidates
                ):
                    candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.host.database in endpoint_hosts
                    ]
                counts.append(len(candidates))
            if 0 in counts:
                missing_attached_endpoint = any(
                    count == 0 and str(ref.get("catalog_id", "")) in attached_catalog_ids
                    for count, ref in zip(counts, (from_ref, to_ref), strict=True)
                )
                status = "unavailable" if missing_attached_endpoint else "unresolved"
            elif any(count > 1 for count in counts):
                status = "ambiguous"
            else:
                status = "resolved"
        relationships[relationship_id] = FederatedRelationship(
            relationship_id=relationship_id,
            definition={key: value for key, value in definition.items() if key != "_host"},
            resolution_status=status,
            attestation=attestation,
            host_aliases=tuple(sorted({alias for alias, _, _ in assertions})),
        )
    return FederatedSemanticModel(entities, relationships, diagnostics)


def _jsonable(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))
