from __future__ import annotations

from migration_law_ingestion.model import Graph, Node, Provenance, Relationship
from migration_law_ingestion.neo4j_sink import write_graph


class Result:
    def consume(self):
        return None


class Transaction:
    def __init__(self, calls):
        self.calls = calls

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return Result()


class Session:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute_write(self, callback):
        return callback(Transaction(self.calls))


class Driver:
    def __init__(self):
        self.calls = []

    def session(self, database=None):
        return Session(self.calls)


def test_neo4j_sink_upserts_allowlisted_labels_and_relationships():
    provenance = Provenance("T", "V", "source.epub", "source#p-1", "text", "2026-01-01", None, "2026-01-01T00:00:00Z", "hash", "0.1", "test", 1.0)
    graph = Graph()
    graph.add_node(Node("title:T", ("LegislationTitle",), {"title_id": "T"}, provenance))
    graph.add_node(Node("version:T:V", ("LegislationVersion",), {"title_id": "T"}, provenance))
    graph.add_relationship(Relationship("rel", "HAS_VERSION", "title:T", "version:T:V", {}, provenance))
    driver = Driver()

    write_graph(driver, graph)

    assert len(driver.calls) == 3
    assert any("LegislationTitle" in query for query, _ in driver.calls)
    assert any("HAS_VERSION" in query for query, _ in driver.calls)
    assert all("provenance_json" in parameters["rows"][0]["properties"] for _, parameters in driver.calls)
