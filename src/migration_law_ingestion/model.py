"""Portable canonical graph objects, independent of any graph database driver."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Provenance:
    source_title_id: str
    source_version_id: str
    source_document: str
    source_location: str
    source_text: str
    effective_from: str | None
    effective_to: str | None
    retrieved_at: str
    source_hash: str
    parser_version: str
    extraction_method: str
    confidence: float


@dataclass(frozen=True)
class Node:
    id: str
    labels: tuple[str, ...]
    properties: dict[str, Any]
    provenance: Provenance


@dataclass(frozen=True)
class Relationship:
    id: str
    type: str
    start_id: str
    end_id: str
    properties: dict[str, Any]
    provenance: Provenance


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        existing = self.nodes.get(node.id)
        if existing and existing != node:
            raise ValueError(f"non-identical node ID: {node.id}")
        self.nodes[node.id] = node

    def add_relationship(self, relationship: Relationship) -> None:
        existing = self.relationships.get(relationship.id)
        if existing and existing != relationship:
            raise ValueError(f"non-identical relationship ID: {relationship.id}")
        self.relationships[relationship.id] = relationship

    def merge(self, other: "Graph") -> None:
        """Combine independently parsed titles while preserving stable identities."""
        for node in other.nodes.values():
            existing = self.nodes.get(node.id)
            if existing is None:
                self.nodes[node.id] = node
            elif existing != node:
                self.nodes[node.id] = Node(
                    node.id,
                    tuple(sorted(set(existing.labels) | set(node.labels))),
                    existing.properties | node.properties,
                    node.provenance,
                )
        for relationship in other.relationships.values():
            self.relationships.setdefault(relationship.id, relationship)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(node) for node in self.nodes.values()],
            "relationships": [asdict(rel) for rel in self.relationships.values()],
        }
