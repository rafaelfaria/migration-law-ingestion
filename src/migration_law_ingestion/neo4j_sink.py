"""Optional Neo4j writer for the canonical graph; no database is required to ingest."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .model import Graph


ALLOWED_LABELS = {
    "LegislationTitle", "LegislationVersion", "Schedule", "Part", "Division", "Subdivision",
    "Provision", "VisaClass", "VisaSubclass", "PIC", "SRC", "VisaCondition", "LegislativeInstrument",
}
ALLOWED_RELATIONSHIPS = {
    "HAS_VERSION", "CONTAINS", "PART_OF", "HAS_SUBCLASS", "HAS_VALIDITY_PROVISION",
    "HAS_GRANT_PROVISION", "REFERENCES", "REFERENCES_PIC", "REFERENCES_SRC", "REFERENCES_CONDITION",
    "AUTHORISED_BY", "SPECIFIED_BY", "AMENDED_BY", "SUPERSEDED_BY",
}
BATCH_SIZE = 500


def _properties(item: Any) -> dict[str, Any]:
    return item.properties | {"provenance_json": json.dumps(asdict(item.provenance), sort_keys=True)}


def write_graph(driver: Any, graph: Graph, database: str | None = None) -> None:
    """Upsert graph data using only core Cypher; labels/types are allow-listed."""
    graph.validate()
    with driver.session(database=database) as session:
        nodes_by_labels: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for node in graph.nodes.values():
            labels = set(node.labels)
            if not labels <= ALLOWED_LABELS:
                raise ValueError(f"unsupported Neo4j labels: {labels}")
            nodes_by_labels[tuple(sorted(labels))].append({"id": node.id, "properties": _properties(node)})
        for labels, rows in nodes_by_labels.items():
            label_clause = ":".join(labels)
            query = f"UNWIND $rows AS row MERGE (n:{label_clause} {{id: row.id}}) SET n += row.properties"
            for start in range(0, len(rows), BATCH_SIZE):
                session.execute_write(lambda tx, q=query, batch=rows[start:start + BATCH_SIZE]: tx.run(q, rows=batch).consume())
        relationships_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relationship in graph.relationships.values():
            if relationship.type not in ALLOWED_RELATIONSHIPS:
                raise ValueError(f"unsupported Neo4j relationship: {relationship.type}")
            relationships_by_type[relationship.type].append({"id": relationship.id, "start_id": relationship.start_id, "end_id": relationship.end_id, "properties": _properties(relationship)})
        for relationship_type, rows in relationships_by_type.items():
            query = (
                "UNWIND $rows AS row MATCH (source {id: row.start_id}) MATCH (target {id: row.end_id}) "
                f"MERGE (source)-[rel:{relationship_type} {{id: row.id}}]->(target) SET rel += row.properties"
            )
            for start in range(0, len(rows), BATCH_SIZE):
                session.execute_write(lambda tx, q=query, batch=rows[start:start + BATCH_SIZE]: tx.run(q, rows=batch).consume())


def write_graph_from_environment(graph: Graph) -> None:
    """Connect only when the caller expressly requests Neo4j persistence."""
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError("install the 'neo4j' extra before enabling Neo4j persistence") from error
    load_dotenv()
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USER")
    password = os.environ.get("NEO4J_PASSWORD")
    database = os.environ.get("NEO4J_DATABASE")
    if not all((uri, user, password)):
        raise RuntimeError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required for Neo4j persistence")
    try:
        from neo4j import GraphDatabase
    except ImportError as error:
        raise RuntimeError("install the 'neo4j' extra before enabling Neo4j persistence") from error
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        write_graph(driver, graph, database)
    finally:
        driver.close()
