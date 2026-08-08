"""Commands for baseline ingestion and daily update checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .api import DownloadedDocument, RegisterApiClient
from .archive import ArchivedObject, RawArchive
from .model import Graph, Node, Provenance, Relationship
from .parser import RegulationsParser


MIGRATION_REGULATIONS_TITLE_ID = "F1996B03551"
MIGRATION_ACT_TITLE_ID = "C1958A00062"


@dataclass(frozen=True)
class SourceTitle:
    title_id: str
    kind: str  # Act | Regulations | LegislativeInstrument
    name: str | None = None


@dataclass
class IngestResult:
    source: SourceTitle
    version_id: str
    graph: Graph
    changed: bool


def _as_date(value: str) -> date:
    return date.fromisoformat(value)


def _primary_documents(version: dict[str, Any], document_format: str) -> list[dict[str, Any]]:
    target = {"Epub": (3, "Epub"), "Pdf": (2, "Pdf")}[document_format]
    return [
        document
        for document in version.get("documents") or []
        if document.get("type") in (0, "Primary") and document.get("format") in target
    ]


def _archive_document(archive: RawArchive, title_id: str, version_id: str, document: DownloadedDocument, fallback_name: str) -> ArchivedObject:
    return archive.store_bytes(
        title_id,
        version_id,
        document.filename or fallback_name,
        document.body,
        request_url=document.request_url,
        content_type=document.content_type,
    )


def _base_graph(title: dict[str, Any], version: dict[str, Any], source: SourceTitle, provenance: Provenance) -> Graph:
    graph = Graph()
    title_node = f"title:{source.title_id}"
    version_id = version.get("registerId") or f"as-at:{version.get('start', 'unknown')}"
    version_node = f"version:{source.title_id}:{version_id}"
    labels = ("LegislationTitle",) + (("LegislativeInstrument",) if source.kind == "LegislativeInstrument" else ())
    graph.add_node(Node(title_node, labels, {"title_id": source.title_id, "name": title.get("name"), "collection": title.get("collection"), "register_status": title.get("status")}, provenance))
    graph.add_node(Node(version_node, ("LegislationVersion",), {"title_id": source.title_id, "register_id": version_id, "compilation_number": version.get("compilationNumber"), "register_status": version.get("status"), "is_current": version.get("isCurrent"), "is_latest": version.get("isLatest"), "has_unincorporated_amendments": version.get("hasUnincorporatedAmendments")}, provenance))
    graph.add_relationship(Relationship(f"rel:HAS_VERSION:{title_node}:{version_node}", "HAS_VERSION", title_node, version_node, {}, provenance))
    for reason in version.get("reasons") or []:
        affected = reason.get("affectedByTitle") or {}
        amendment_title = affected.get("titleId")
        if amendment_title:
            amendment = f"title:{amendment_title}"
            graph.add_node(Node(amendment, ("LegislationTitle",), {"title_id": amendment_title, "name": affected.get("name")}, provenance))
            graph.add_relationship(Relationship(f"rel:AMENDED_BY:{version_node}:{amendment}", "AMENDED_BY", version_node, amendment, {"provisions": affected.get("provisions")}, provenance))
    return graph


def _add_authority(graph: Graph, source: SourceTitle, title: dict[str, Any], provenance: Provenance) -> None:
    title_node = f"title:{source.title_id}"
    authority_ids: set[str] = set()
    if source.title_id == MIGRATION_REGULATIONS_TITLE_ID:
        authority_ids.add(MIGRATION_ACT_TITLE_ID)
    for affect in title.get("authorisedBy") or []:
        authority_ids.add(affect.get("affectingTitleId") or "")
    for authority_id in authority_ids - {""}:
        target = f"title:{authority_id}"
        if target not in graph.nodes:
            graph.add_node(Node(target, ("LegislationTitle",), {"title_id": authority_id, "resolved": authority_id == MIGRATION_ACT_TITLE_ID}, provenance))
        graph.add_relationship(Relationship(f"rel:AUTHORISED_BY:{title_node}:{target}", "AUTHORISED_BY", title_node, target, {}, provenance))


def _link_known_predecessor(graph: Graph, archive: RawArchive, source: SourceTitle, version: dict[str, Any], version_id: str, provenance: Provenance) -> None:
    predecessor = archive.previous_version(source.title_id, version_id, version.get("start"))
    if predecessor is None:
        return
    previous, metadata_path = predecessor
    previous_id = previous.get("registerId") or metadata_path.parent.parent.name
    title_node = f"title:{source.title_id}"
    previous_node = f"version:{source.title_id}:{previous_id}"
    previous_bytes = metadata_path.read_bytes()
    previous_provenance = Provenance(
        source.title_id,
        previous_id,
        "version.json",
        str(metadata_path),
        metadata_path.read_text(encoding="utf-8"),
        previous.get("start"),
        previous.get("end"),
        provenance.retrieved_at,
        hashlib.sha256(previous_bytes).hexdigest(),
        "0.1.0",
        "register-version-json",
        1.0,
    )
    graph.add_node(Node(previous_node, ("LegislationVersion",), {"title_id": source.title_id, "register_id": previous_id, "compilation_number": previous.get("compilationNumber")}, previous_provenance))
    graph.add_relationship(Relationship(f"rel:HAS_VERSION:{title_node}:{previous_node}", "HAS_VERSION", title_node, previous_node, {}, previous_provenance))
    graph.add_relationship(Relationship(f"rel:SUPERSEDED_BY:{previous_node}:version:{source.title_id}:{version_id}", "SUPERSEDED_BY", previous_node, f"version:{source.title_id}:{version_id}", {}, Provenance(source.title_id, version_id, provenance.source_document, provenance.source_location, "version interval order", provenance.effective_from, provenance.effective_to, provenance.retrieved_at, provenance.source_hash, "0.1.0", "version-interval-order-v1", 0.95)))


def ingest_title(
    client: RegisterApiClient,
    archive: RawArchive,
    source: SourceTitle,
    as_at: date,
    include_pdf: bool,
) -> IngestResult:
    """Archive exactly the version selected by the Register and produce its graph."""
    title, title_url = client.get_title(source.title_id, include_authority=True)
    version, version_url = client.get_version(source.title_id, as_at)
    version_id = version.get("registerId") or f"as-at-{as_at.isoformat()}"
    title_archive = archive.store_json(source.title_id, version_id, "title", title, title_url)
    version_archive = archive.store_json(source.title_id, version_id, "version", version, version_url)
    changed = not (title_archive.reused and version_archive.reused)
    entries = [title_archive, version_archive]

    epub_path = archive.latest_document(source.title_id, version_id, ".epub")
    epub_object: ArchivedObject | None = None
    if _primary_documents(version, "Epub") and (changed or epub_path is None):
        epub = client.download_primary_document(source.title_id, as_at, "Epub")
        epub_object = _archive_document(archive, source.title_id, version_id, epub, f"{version_id}.epub")
        entries.append(epub_object)
        epub_path = epub_object.path
        changed = changed or not epub_object.reused

    if include_pdf and (changed or not archive.latest_document(source.title_id, version_id, ".pdf")):
        for document in _primary_documents(version, "Pdf"):
            volume = document.get("volumeNumber", 0)
            pdf = client.download_primary_document(source.title_id, as_at, "Pdf", volume)
            entries.append(_archive_document(archive, source.title_id, version_id, pdf, f"{version_id}-v{volume}.pdf"))

    if changed:
        archive.write_manifest(source.title_id, version_id, entries)

    if epub_path:
        epub_bytes = epub_path.read_bytes()
        source_hash = hashlib.sha256(epub_bytes).hexdigest()
        retrieved_at = epub_object.retrieved_at if epub_object else version_archive.retrieved_at
        provenance = Provenance(source.title_id, version_id, epub_path.name, str(epub_path), "", version.get("start"), version.get("end"), retrieved_at, source_hash, "0.1.0", "register-epub", 1.0)
        if source.title_id == MIGRATION_REGULATIONS_TITLE_ID:
            graph = RegulationsParser().parse_epub(epub_bytes, title_id=source.title_id, version_id=version_id, effective_from=version.get("start"), effective_to=version.get("end"), retrieved_at=retrieved_at, source_hash=source_hash)
            graph.merge(_base_graph(title, version, source, provenance))
        else:
            graph = _base_graph(title, version, source, provenance)
    else:
        provenance = Provenance(source.title_id, version_id, "register-api", version_url, "", version.get("start"), version.get("end"), version_archive.retrieved_at, version_archive.sha256, "0.1.0", "register-version-json", 1.0)
        graph = _base_graph(title, version, source, provenance)
    _add_authority(graph, source, title, provenance)
    _link_known_predecessor(graph, archive, source, version, version_id, provenance)
    return IngestResult(source, version_id, graph, changed)


def discover_migration_instruments(client: RegisterApiClient, limit: int | None = None) -> list[SourceTitle]:
    """Discover current principal migration instruments from the Register catalogue."""
    candidates, _ = client.list_in_force_migration_titles()
    instruments = [
        SourceTitle(title["id"], "LegislativeInstrument", title.get("name"))
        for title in candidates
        if title.get("id") != MIGRATION_REGULATIONS_TITLE_ID
        and title.get("collection") == "LegislativeInstrument"
        and title.get("isPrincipal") is True
        # Catalogue text search also catches unrelated terms such as aircraft fuel
        # migration. Keep Phase 1's title-only discovery boundary conservative.
        and re.match(r"^Migration(?:\s|\(|$)", title.get("name") or "", re.I)
    ]
    instruments.sort(key=lambda item: item.title_id)
    return instruments if limit is None else instruments[:limit]


def baseline_sources(client: RegisterApiClient, include_instruments: bool, instrument_limit: int | None) -> list[SourceTitle]:
    sources = [SourceTitle(MIGRATION_ACT_TITLE_ID, "Act", "Migration Act 1958"), SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations", "Migration Regulations 1994")]
    if include_instruments:
        sources.extend(discover_migration_instruments(client, instrument_limit))
    return sources


def ingest_baseline(as_at: date, archive_root: Path, output: Path, include_instruments: bool = False, instrument_limit: int | None = None, skip_pdf: bool = False) -> list[IngestResult]:
    client = RegisterApiClient()
    archive = RawArchive(archive_root)
    graph = Graph()
    results = []
    for source in baseline_sources(client, include_instruments, instrument_limit):
        result = ingest_title(client, archive, source, as_at, include_pdf=not skip_pdf)
        results.append(result)
        graph.merge(result.graph)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return results


def ingest_regulations(as_at: date, archive_root: Path, output: Path, skip_pdf: bool = False) -> None:
    """Compatibility command retained for the focused Subclass 102 acceptance slice."""
    client = RegisterApiClient()
    result = ingest_title(client, RawArchive(archive_root), SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations"), as_at, include_pdf=not skip_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_updates(as_at: date, archive_root: Path, include_instruments: bool, instrument_limit: int | None) -> list[dict[str, str | bool]]:
    client = RegisterApiClient()
    archive = RawArchive(archive_root)
    updates = []
    for source in baseline_sources(client, include_instruments, instrument_limit):
        title, _ = client.get_title(source.title_id, include_authority=True)
        version, _ = client.get_version(source.title_id, as_at)
        version_id = version.get("registerId") or f"as-at-{as_at.isoformat()}"
        changed = not (
            archive.has_json_payload(source.title_id, version_id, "title", title)
            and archive.has_json_payload(source.title_id, version_id, "version", version)
        )
        updates.append({"title_id": source.title_id, "version_id": version_id, "changed": changed})
    return updates


def _add_common_arguments(command: argparse.ArgumentParser, include_instruments: bool = False) -> None:
    command.add_argument("--as-at", required=True, type=_as_date)
    command.add_argument("--archive-root", type=Path, default=Path("data/raw"))
    if include_instruments:
        command.add_argument("--include-instruments", action="store_true", help="discover current principal migration instruments from the Register")
        command.add_argument("--instrument-limit", type=int, default=None, help="cap discovered instruments for a bounded run")


def main() -> None:
    parser = argparse.ArgumentParser(prog="migration-law-ingest")
    command = parser.add_subparsers(dest="command", required=True)
    regulations = command.add_parser("ingest-regulations", help="archive and parse Migration Regulations 1994")
    _add_common_arguments(regulations)
    regulations.add_argument("--output", type=Path, default=Path("data/graph.json"))
    regulations.add_argument("--skip-pdf", action="store_true")
    baseline = command.add_parser("ingest-baseline", help="ingest the Act, Regulations and optionally current migration instruments")
    _add_common_arguments(baseline, include_instruments=True)
    baseline.add_argument("--output", type=Path, default=Path("data/graph.json"))
    baseline.add_argument("--skip-pdf", action="store_true")
    updates = command.add_parser("check-updates", help="report versions not yet present in the immutable archive")
    _add_common_arguments(updates, include_instruments=True)
    args = parser.parse_args()
    if args.command == "ingest-regulations":
        ingest_regulations(args.as_at, args.archive_root, args.output, args.skip_pdf)
    elif args.command == "ingest-baseline":
        results = ingest_baseline(args.as_at, args.archive_root, args.output, args.include_instruments, args.instrument_limit, args.skip_pdf)
        print(json.dumps({"ingested": len(results), "changed": sum(result.changed for result in results)}, indent=2))
    elif args.command == "check-updates":
        print(json.dumps(check_updates(args.as_at, args.archive_root, args.include_instruments, args.instrument_limit), indent=2))


if __name__ == "__main__":
    main()
