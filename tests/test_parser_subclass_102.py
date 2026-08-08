from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from migration_law_ingestion.parser import RegulationsParser


def fixture_epub() -> bytes:
    html = (Path(__file__).parent / "fixtures" / "subclass_102.epub.html").read_bytes()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("OEBPS/document_1/document_1.html", html)
    return output.getvalue()


def parse_fixture():
    return RegulationsParser().parse_epub(
        fixture_epub(),
        title_id="F1996B03551",
        version_id="F2026C00667",
        effective_from="2026-07-01T00:00:00",
        effective_to=None,
        retrieved_at="2026-08-08T00:00:00Z",
        source_hash="fixture-hash",
    )


def relationship_types(graph, start, end):
    return {relationship.type for relationship in graph.relationships.values() if relationship.start_id == start and relationship.end_id == end}


def test_subclass_102_reconstructs_validity_and_grant_pathway():
    graph = parse_fixture()
    visa_class = "visa-class:F1996B03551:F2026C00667:1108"
    subclass = "visa-subclass:F1996B03551:F2026C00667:102"
    validity_item = "provision:F1996B03551:F2026C00667:item-1108"
    grant_clause = "provision:F1996B03551:F2026C00667:102.221"

    assert "HAS_SUBCLASS" in relationship_types(graph, visa_class, subclass)
    assert "HAS_VALIDITY_PROVISION" in relationship_types(graph, subclass, validity_item)
    assert "HAS_GRANT_PROVISION" in relationship_types(graph, subclass, grant_clause)
    assert any("Division" in node.labels for node in graph.nodes.values())
    assert any("Subdivision" in node.labels for node in graph.nodes.values())


def test_subclass_102_extracts_pics_conditions_and_instrument_hooks_with_provenance():
    graph = parse_fixture()
    grant_clause = "provision:F1996B03551:F2026C00667:102.221"
    assert "REFERENCES_PIC" in relationship_types(graph, grant_clause, "pic:F1996B03551:F2026C00667:4001")
    assert "REFERENCES_PIC" in relationship_types(graph, grant_clause, "pic:F1996B03551:F2026C00667:4020")
    assert "REFERENCES_CONDITION" in relationship_types(graph, "provision:F1996B03551:F2026C00667:102.611", "condition:F1996B03551:F2026C00667:8501")
    assert any(relationship.type == "SPECIFIED_BY" and relationship.start_id.endswith("item-1108") for relationship in graph.relationships.values())
    assert all(node.provenance.source_title_id == "F1996B03551" for node in graph.nodes.values())
    assert all(relationship.provenance.source_hash == "fixture-hash" for relationship in graph.relationships.values())
