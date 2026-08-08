from __future__ import annotations

from datetime import date
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from migration_law_ingestion.api import DownloadedDocument
from migration_law_ingestion.archive import RawArchive
from migration_law_ingestion.cli import (
    MIGRATION_ACT_TITLE_ID,
    MIGRATION_REGULATIONS_TITLE_ID,
    SourceTitle,
    backfill_title,
    discover_migration_instruments,
    ingest_title,
    historical_sources,
    ingest_version_metadata,
    check_updates,
)
from migration_law_ingestion.cli import _base_graph
from migration_law_ingestion.model import Provenance


def epub_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("OEBPS/document_1/document_1.html", "<p class='ActHead1'>Schedule 1</p>")
    return output.getvalue()


class FakeClient:
    def __init__(self):
        self.downloads = 0

    def get_title(self, title_id, include_authority=False):
        authority = [{"affectingTitleId": MIGRATION_ACT_TITLE_ID}] if title_id == "F2026L00001" else []
        return ({"id": title_id, "name": "Migration Test Instrument" if title_id == "F2026L00001" else f"Title {title_id}", "collection": "Act", "status": "InForce", "authorisedBy": authority}, f"https://example.test/titles/{title_id}")

    def get_version(self, title_id, as_at):
        return ({"registerId": f"{title_id}-v1", "start": "2026-01-01T00:00:00", "end": None, "documents": [{"type": "Primary", "format": "Epub", "volumeNumber": 0}], "reasons": []}, f"https://example.test/versions/{title_id}")

    def download_primary_document(self, title_id, as_at, document_format, volume_number=0, unique_type_number=0):
        self.downloads += 1
        return DownloadedDocument(epub_bytes(), "application/epub+zip", f"{title_id}.epub", "https://example.test/document")

    def list_in_force_migration_titles(self):
        return ([
            {"id": MIGRATION_REGULATIONS_TITLE_ID, "collection": "LegislativeInstrument", "isPrincipal": True},
            {"id": "F2026L00001", "name": "Migration Test Instrument", "collection": "LegislativeInstrument", "isPrincipal": True},
            {"id": "F2026L00002", "name": "Migration Amendment", "collection": "LegislativeInstrument", "isPrincipal": False},
        ], "https://example.test/titles")


class VersioningClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.version_number = 1

    def get_version(self, title_id, as_at):
        return ({"registerId": f"{title_id}-v{self.version_number}", "start": f"2026-0{self.version_number}-01T00:00:00", "end": None, "documents": [{"type": "Primary", "format": "Epub", "volumeNumber": 0}], "reasons": []}, f"https://example.test/versions/{title_id}")


class BackfillClient(FakeClient):
    def list_versions(self, title_id):
        return [{"registerId": "T-v1", "start": "2025-01-01T00:00:00"}, {"registerId": "T-v2", "start": "2026-01-01T00:00:00"}]

    def get_version_by_register_id(self, register_id):
        return ({"registerId": register_id, "start": "2025-01-01T00:00:00", "end": None, "documents": [{"type": "Primary", "format": "Epub", "volumeNumber": 0}], "reasons": []}, f"https://example.test/versions/{register_id}")

    def download_primary_document_by_register_id(self, register_id, document_format, volume_number=0, unique_type_number=0):
        self.downloads += 1
        return DownloadedDocument(epub_bytes(), "application/epub+zip", f"{register_id}.epub", "https://example.test/document")


def test_title_catalogue_follows_count_based_pagination(monkeypatch):
    from migration_law_ingestion.api import RegisterApiClient

    client = RegisterApiClient()
    responses = [
        ({"@odata.count": 3, "value": [{"id": "one"}, {"id": "two"}]}, "https://example.test/first"),
        ({"@odata.count": 3, "value": [{"id": "three"}]}, "https://example.test/second"),
    ]
    monkeypatch.setattr(client, "get_json", lambda _: responses.pop(0))

    titles, url = client.list_migration_titles(in_force_only=False, page_size=2)

    assert [title["id"] for title in titles] == ["one", "two", "three"]
    assert url == "https://example.test/first"


def test_title_ingestion_reuses_archived_epub_without_a_second_download(tmp_path):
    client = FakeClient()
    archive = RawArchive(tmp_path / "raw")
    source = SourceTitle(MIGRATION_ACT_TITLE_ID, "Act")
    first = ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)
    second = ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)

    assert first.changed is True
    assert second.changed is False
    assert client.downloads == 1
    assert "title:C1958A00062" in second.graph.nodes


def test_instrument_discovery_keeps_only_current_principal_instruments():
    instruments = discover_migration_instruments(FakeClient())
    assert instruments == [SourceTitle("F2026L00001", "LegislativeInstrument", "Migration Test Instrument")]


def test_historical_catalogue_can_start_with_the_two_root_laws_without_discovery():
    assert historical_sources(FakeClient(), instrument_limit=0) == [
        SourceTitle(MIGRATION_ACT_TITLE_ID, "Act", "Migration Act 1958"),
        SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations", "Migration Regulations 1994"),
    ]


def test_multiple_amendment_reasons_from_one_title_remain_distinct():
    source = SourceTitle("T", "Act")
    provenance = Provenance("T", "V", "version.json", "test", "", None, None, "2026-01-01T00:00:00Z", "hash", "test", "test", 1.0)
    graph = _base_graph(
        {"name": "Test"},
        {
            "registerId": "V",
            "reasons": [
                {"affectedByTitle": {"titleId": "A", "provisions": "item 1"}},
                {"affectedByTitle": {"titleId": "A", "provisions": "item 2"}},
            ],
        },
        source,
        provenance,
    )

    assert sum(rel.type == "AMENDED_BY" for rel in graph.relationships.values()) == 2


def test_repeated_amendment_title_with_inconsistent_display_name_is_idempotent():
    source = SourceTitle("T", "Act")
    provenance = Provenance("T", "V", "version.json", "test", "", None, None, "2026-01-01T00:00:00Z", "hash", "test", "test", 1.0)
    graph = _base_graph(
        {"name": "Test"},
        {
            "registerId": "V",
            "reasons": [
                {"affectedByTitle": {"titleId": "A", "name": "First name", "provisions": "item 1"}},
                {"affectedByTitle": {"titleId": "A", "name": "Second name", "provisions": "item 2"}},
            ],
        },
        source,
        provenance,
    )

    assert graph.nodes["title:A"].properties["name"] == "First name"
    assert sum(rel.type == "AMENDED_BY" for rel in graph.relationships.values()) == 2


def test_historical_row_without_register_id_is_retained_as_metadata(tmp_path):
    archive = RawArchive(tmp_path / "raw")
    source = SourceTitle("T", "Act")
    result = ingest_version_metadata(
        archive,
        source,
        {"name": "Title T", "authorisedBy": []},
        "https://example.test/title",
        {"start": "1990-01-01T00:00:00", "end": "1990-02-01T00:00:00", "reasons": []},
        "https://example.test/versions",
    )

    assert result.version_id == "as-at-1990-01-01"
    version = result.graph.nodes["version:T:as-at-1990-01-01"]
    assert version.provenance.extraction_method == "register-version-metadata-only"


def test_as_at_ingestion_uses_requested_identity_when_register_version_has_no_id(tmp_path):
    client = FakeClient()
    client.get_version = lambda title_id, as_at: ({"start": "2026-08-08T00:00:00", "end": None, "documents": [], "reasons": []}, "https://example.test/version")
    result = ingest_title(client, RawArchive(tmp_path / "raw"), SourceTitle("T", "Act"), date(2026, 8, 9), include_pdf=False)

    assert result.version_id == "as-at-2026-08-09"
    assert "version:T:as-at-2026-08-09" in result.graph.nodes


def test_update_check_requires_a_successful_baseline_receipt(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("migration_law_ingestion.cli.RegisterApiClient", lambda **_: client)
    archive = RawArchive(tmp_path / "raw")
    output = tmp_path / "graph.json"
    entries = []
    for title_id in (MIGRATION_ACT_TITLE_ID, MIGRATION_REGULATIONS_TITLE_ID):
        title, title_url = client.get_title(title_id, include_authority=True)
        version, version_url = client.get_version(title_id, date(2026, 8, 8))
        archive.store_json(title_id, version["registerId"], "title", title, title_url)
        archive.store_json(title_id, version["registerId"], "version", version, version_url)
        entries.append({"title_id": title_id, "version_id": version["registerId"]})
    output.with_name("source-registry.json").write_text(__import__("json").dumps({"titles": entries}), encoding="utf-8")

    assert not any(item["changed"] for item in check_updates(date(2026, 8, 8), tmp_path / "raw", False, None, output=output))
    output.with_name("source-registry.json").unlink()
    assert all(item["changed"] for item in check_updates(date(2026, 8, 8), tmp_path / "raw", False, None, output=output))


def test_newer_archived_version_is_linked_as_superseding_its_predecessor(tmp_path):
    client = VersioningClient()
    archive = RawArchive(tmp_path / "raw")
    source = SourceTitle(MIGRATION_ACT_TITLE_ID, "Act")
    ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)
    client.version_number = 2
    second = ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)

    assert any(relationship.type == "SUPERSEDED_BY" for relationship in second.graph.relationships.values())


def test_backfill_uses_register_ids_to_preserve_distinct_historical_versions(tmp_path, monkeypatch):
    client = BackfillClient()
    monkeypatch.setattr("migration_law_ingestion.cli.RegisterApiClient", lambda **_: client)
    results = backfill_title("T", tmp_path / "raw", tmp_path / "graph.json", source_kind="Act", version_limit=2, skip_pdf=True)

    assert [result.version_id for result in results] == ["T-v1", "T-v2"]
    assert client.downloads == 2
