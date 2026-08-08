from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from migration_law_ingestion.parser import GenericLegislationParser


def test_generic_parser_builds_act_structure_and_citations():
    html = """<html><body>
    <p class='ActHead2'>Part 2—Visas</p>
    <p class='ActHead3'>Division 3—Visas for non-citizens</p>
    <p class='ActHead4'>Subdivision A—General provisions</p>
    <p class='ActHead5'>31 Classes of visas</p>
    <p class='subsection'>A visa is of a class prescribed by the regulations under section 504.</p>
    <p class='ActHead5'>42 Visa essential for travel</p>
    <p class='subsection'>The Minister may specify a matter in a legislative instrument made under subsection 42(1).</p>
    </body></html>"""
    epub = BytesIO()
    with ZipFile(epub, "w", ZIP_DEFLATED) as package:
        package.writestr("OEBPS/document_1/document_1.html", html)
    graph = GenericLegislationParser().parse_epub(
        epub.getvalue(), title_id="C1958A00062", version_id="C2026C00232", effective_from="2026-06-04T00:00:00", effective_to=None, retrieved_at="2026-08-08T00:00:00Z", source_hash="fixture"
    )

    assert "part:C1958A00062:C2026C00232:2" in graph.nodes
    assert "division:C1958A00062:C2026C00232:part:C1958A00062:C2026C00232:2:3" in graph.nodes
    assert any(node.properties.get("number") == "31" for node in graph.nodes.values())
    assert any(relationship.type == "REFERENCES" for relationship in graph.relationships.values())
    assert any(relationship.type == "SPECIFIED_BY" for relationship in graph.relationships.values())


def test_generic_parser_keeps_same_number_in_different_structural_scopes_distinct():
    html = """<html><body>
    <p class='ActHead2'>Part 1—Main text</p><p class='ActHead5'>1 Name</p>
    <p class='ActHead1'>Schedule 1—Additional matters</p><p class='ActHead5'>1 Definitions</p>
    </body></html>"""
    epub = BytesIO()
    with ZipFile(epub, "w", ZIP_DEFLATED) as package:
        package.writestr("OEBPS/document_1/document_1.html", html)
    graph = GenericLegislationParser().parse_epub(epub.getvalue(), title_id="F", version_id="V", effective_from=None, effective_to=None, retrieved_at="2026-08-08T00:00:00Z", source_hash="fixture")
    matches = [node for node in graph.nodes.values() if "Provision" in node.labels and node.properties.get("number") == "1"]
    assert len(matches) == 2
    assert len({node.properties["structural_parent_id"] for node in matches}) == 2
