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
    discover_migration_instruments,
    ingest_title,
)


def epub_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("OEBPS/document_1/document_1.html", "<p class='ActHead1'>Schedule 1</p>")
    return output.getvalue()


class FakeClient:
    def __init__(self):
        self.downloads = 0

    def get_title(self, title_id, include_authority=False):
        return ({"id": title_id, "name": f"Title {title_id}", "collection": "Act", "status": "InForce", "authorisedBy": []}, f"https://example.test/titles/{title_id}")

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


def test_newer_archived_version_is_linked_as_superseding_its_predecessor(tmp_path):
    client = VersioningClient()
    archive = RawArchive(tmp_path / "raw")
    source = SourceTitle(MIGRATION_ACT_TITLE_ID, "Act")
    ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)
    client.version_number = 2
    second = ingest_title(client, archive, source, date(2026, 8, 8), include_pdf=False)

    assert any(relationship.type == "SUPERSEDED_BY" for relationship in second.graph.relationships.values())
