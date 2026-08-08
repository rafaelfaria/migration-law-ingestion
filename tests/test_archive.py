from migration_law_ingestion.archive import RawArchive


def test_content_addressed_archive_reuses_identical_bytes(tmp_path):
    archive = RawArchive(tmp_path)
    first = archive.store_bytes("F1996B03551", "F2026C00667", "source.epub", b"source")
    second = archive.store_bytes("F1996B03551", "F2026C00667", "source.epub", b"source")
    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == b"source"
