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
from .neo4j_sink import write_graph_from_environment
from .parser import GenericLegislationParser, RegulationsParser


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


def _base_graph(title: dict[str, Any], version: dict[str, Any], source: SourceTitle, provenance: Provenance, canonical_version_id: str | None = None) -> Graph:
    graph = Graph()
    title_node = f"title:{source.title_id}"
    version_id = canonical_version_id or version.get("registerId") or _point_in_time_id(version)
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
            provisions = affected.get("provisions")
            reason_key = hashlib.sha256(json.dumps(provisions, sort_keys=True).encode("utf-8")).hexdigest()[:12]
            graph.add_relationship(Relationship(f"rel:AMENDED_BY:{version_node}:{amendment}:{reason_key}", "AMENDED_BY", version_node, amendment, {"provisions": provisions}, provenance))
    return graph


def _add_authority(graph: Graph, source: SourceTitle, title: dict[str, Any], provenance: Provenance) -> None:
    title_node = f"title:{source.title_id}"
    authority_ids: dict[str, str | None] = {}
    if source.title_id == MIGRATION_REGULATIONS_TITLE_ID:
        authority_ids[MIGRATION_ACT_TITLE_ID] = "s 504"
    for affect in title.get("authorisedBy") or []:
        authority_id = affect.get("affectingTitleId")
        if authority_id:
            authority_ids[authority_id] = affect.get("affectingProvisions")
    for authority_id, provisions in authority_ids.items():
        target = f"title:{authority_id}"
        if target not in graph.nodes:
            graph.add_node(Node(target, ("LegislationTitle",), {"title_id": authority_id, "resolved": authority_id == MIGRATION_ACT_TITLE_ID}, provenance))
        graph.add_relationship(Relationship(f"rel:AUTHORISED_BY:{title_node}:{target}", "AUTHORISED_BY", title_node, target, {"affecting_provisions": provisions}, provenance))


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


def _point_in_time_id(version: dict[str, Any]) -> str:
    """Stable identity for a Register interval that has no Register ID."""
    start = version.get("start")
    if not start:
        raise ValueError("Register version row has neither registerId nor start")
    return f"as-at-{start[:10]}"


def ingest_version_metadata(
    archive: RawArchive,
    source: SourceTitle,
    title: dict[str, Any],
    title_url: str,
    version: dict[str, Any],
    version_url: str,
) -> IngestResult:
    """Persist a legacy Register point-in-time row where no source document exists."""
    version_id = _point_in_time_id(version)
    title_archive = archive.store_json(source.title_id, version_id, "title", title, title_url)
    version_archive = archive.store_json(source.title_id, version_id, "version", version, version_url)
    provenance = Provenance(
        source.title_id,
        version_id,
        "version.json",
        str(version_archive.path),
        "",
        version.get("start"),
        version.get("end"),
        version_archive.retrieved_at,
        version_archive.sha256,
        "0.1.0",
        "register-version-metadata-only",
        1.0,
    )
    graph = _base_graph(title, version, source, provenance, canonical_version_id=version_id)
    _add_authority(graph, source, title, provenance)
    _link_known_predecessor(graph, archive, source, version, version_id, provenance)
    archive.write_manifest(source.title_id, version_id, [title_archive, version_archive])
    return IngestResult(source, version_id, graph, not (title_archive.reused and version_archive.reused))


def ingest_title(
    client: RegisterApiClient,
    archive: RawArchive,
    source: SourceTitle,
    as_at: date | None,
    include_pdf: bool,
    register_id: str | None = None,
    title_payload: dict[str, Any] | None = None,
    title_url: str | None = None,
) -> IngestResult:
    """Archive exactly the version selected by the Register and produce its graph."""
    if title_payload is None or title_url is None:
        title, resolved_title_url = client.get_title(source.title_id, include_authority=True)
    else:
        title, resolved_title_url = title_payload, title_url
    version, version_url = client.get_version_by_register_id(register_id) if register_id else client.get_version(source.title_id, as_at)
    version_id = version.get("registerId") or f"as-at-{as_at.isoformat()}"
    title_archive = archive.store_json(source.title_id, version_id, "title", title, resolved_title_url)
    version_archive = archive.store_json(source.title_id, version_id, "version", version, version_url)
    changed = not (title_archive.reused and version_archive.reused)
    entries = [title_archive, version_archive]

    epub_path = archive.latest_document(source.title_id, version_id, ".epub")
    epub_object: ArchivedObject | None = None
    def download(document_format: str, volume: int = 0) -> DownloadedDocument:
        if register_id:
            return client.download_primary_document_by_register_id(register_id, document_format, volume)
        if as_at is None:
            raise ValueError("as_at is required when register_id is not supplied")
        return client.download_primary_document(source.title_id, as_at, document_format, volume)

    if _primary_documents(version, "Epub") and (changed or epub_path is None):
        epub = download("Epub")
        epub_object = _archive_document(archive, source.title_id, version_id, epub, f"{version_id}.epub")
        entries.append(epub_object)
        epub_path = epub_object.path
        changed = changed or not epub_object.reused

    if include_pdf and (changed or not archive.latest_document(source.title_id, version_id, ".pdf")):
        for document in _primary_documents(version, "Pdf"):
            volume = document.get("volumeNumber", 0)
            pdf = download("Pdf", volume)
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
        else:
            graph = GenericLegislationParser().parse_epub(epub_bytes, title_id=source.title_id, version_id=version_id, effective_from=version.get("start"), effective_to=version.get("end"), retrieved_at=retrieved_at, source_hash=source_hash)
        graph.merge(_base_graph(title, version, source, provenance))
    else:
        provenance = Provenance(source.title_id, version_id, "register-api", version_url, "", version.get("start"), version.get("end"), version_archive.retrieved_at, version_archive.sha256, "0.1.0", "register-version-json", 1.0)
        graph = _base_graph(title, version, source, provenance)
    _add_authority(graph, source, title, provenance)
    _link_known_predecessor(graph, archive, source, version, version_id, provenance)
    return IngestResult(source, version_id, graph, changed)


def discover_migration_instruments(client: RegisterApiClient, limit: int | None = None, include_not_in_force: bool = False) -> list[SourceTitle]:
    """Discover principal Migration instruments authorised by the Act or Regulations.

    The daily catalogue intentionally uses current instruments only. Historical
    backfill opts into the full Register catalogue so former instruments remain
    part of the legal record.
    """
    if include_not_in_force:
        candidates, _ = client.list_migration_titles(in_force_only=False)
    else:
        candidates, _ = client.list_in_force_migration_titles()
    instruments = []
    for candidate in candidates:
        if (
            candidate.get("id") == MIGRATION_REGULATIONS_TITLE_ID
            or candidate.get("collection") != "LegislativeInstrument"
            or candidate.get("isPrincipal") is not True
            or not re.match(r"^Migration(?:\s|\(|$)", candidate.get("name") or "", re.I)
        ):
            continue
        title, _ = client.get_title(candidate["id"], include_authority=True)
        authorities = {affect.get("affectingTitleId") for affect in title.get("authorisedBy") or []}
        if authorities & {MIGRATION_ACT_TITLE_ID, MIGRATION_REGULATIONS_TITLE_ID}:
            instruments.append(SourceTitle(candidate["id"], "LegislativeInstrument", title.get("name")))
    instruments.sort(key=lambda item: item.title_id)
    return instruments if limit is None else instruments[:limit]


def baseline_sources(client: RegisterApiClient, include_instruments: bool, instrument_limit: int | None) -> list[SourceTitle]:
    sources = [SourceTitle(MIGRATION_ACT_TITLE_ID, "Act", "Migration Act 1958"), SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations", "Migration Regulations 1994")]
    if include_instruments:
        sources.extend(discover_migration_instruments(client, instrument_limit))
    return sources


def historical_sources(client: RegisterApiClient, instrument_limit: int | None) -> list[SourceTitle]:
    """Full Phase 1 source catalogue, including former principal instruments."""
    sources = [
        SourceTitle(MIGRATION_ACT_TITLE_ID, "Act", "Migration Act 1958"),
        SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations", "Migration Regulations 1994"),
    ]
    if instrument_limit == 0:
        return sources
    sources.extend(discover_migration_instruments(client, instrument_limit, include_not_in_force=True))
    return sources


def ingest_baseline(as_at: date, archive_root: Path, output: Path, include_instruments: bool = False, instrument_limit: int | None = None, skip_pdf: bool = False, neo4j: bool = False, request_interval: float = 0.75) -> list[IngestResult]:
    client = RegisterApiClient(request_interval_seconds=request_interval)
    archive = RawArchive(archive_root)
    graph = Graph()
    results = []
    for source in baseline_sources(client, include_instruments, instrument_limit):
        result = ingest_title(client, archive, source, as_at, include_pdf=not skip_pdf)
        results.append(result)
        graph.merge(result.graph)
    graph.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    registry = {
        "as_at": as_at.isoformat(),
        "selection": "Migration Act 1958 and Migration Regulations 1994; plus current principal Migration instruments authorised by either root" if include_instruments else "Migration Act 1958 and Migration Regulations 1994",
        "titles": [{"title_id": result.source.title_id, "kind": result.source.kind, "name": result.source.name, "version_id": result.version_id} for result in results],
    }
    output.with_name("source-registry.json").write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if neo4j:
        write_graph_from_environment(graph)
    return results


def backfill_title(
    title_id: str,
    archive_root: Path,
    output: Path,
    source_kind: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    version_limit: int | None = None,
    skip_pdf: bool = False,
    neo4j: bool = False,
    request_interval: float = 0.75,
    client: RegisterApiClient | None = None,
) -> list[IngestResult]:
    """Backfill Register versions without pretending a later compilation is historical text."""
    client = client or RegisterApiClient(request_interval_seconds=request_interval)
    archive = RawArchive(archive_root)
    kind = source_kind or ("Act" if title_id == MIGRATION_ACT_TITLE_ID else "Regulations" if title_id == MIGRATION_REGULATIONS_TITLE_ID else "LegislativeInstrument")
    source = SourceTitle(title_id, kind)
    title, title_url = client.get_title(title_id, include_authority=True)
    versions = client.list_versions(title_id)
    selected = [
        version for version in versions
        if version.get("start")
        and (from_date is None or date.fromisoformat(version["start"][:10]) >= from_date)
        and (to_date is None or date.fromisoformat(version["start"][:10]) <= to_date)
    ]
    if version_limit is not None:
        selected = selected[:version_limit]
    graph = Graph()
    results = []
    for version in selected:
        if version.get("registerId"):
            result = ingest_title(
                client,
                archive,
                source,
                None,
                include_pdf=not skip_pdf,
                register_id=version["registerId"],
                title_payload=title,
                title_url=title_url,
            )
        else:
            result = ingest_version_metadata(
                archive,
                source,
                title,
                title_url,
                version,
                f"register://Versions?titleId={title_id}&start={version['start']}",
            )
        graph.merge(result.graph)
        results.append(result)
    graph.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if neo4j:
        write_graph_from_environment(graph)
    return results


def backfill_baseline(
    archive_root: Path,
    output_root: Path,
    from_date: date | None = None,
    to_date: date | None = None,
    instrument_limit: int | None = None,
    skip_pdf: bool = False,
    neo4j: bool = False,
    request_interval: float = 0.75,
    reprocess_complete: bool = False,
) -> list[dict[str, Any]]:
    """Resumably materialise every historical version in the Phase 1 catalogue.

    A completed title is checkpointed only after all of its Register versions are
    archived and persisted. Re-running safely reuses immutable raw objects and
    starts at the first unfinished title.
    """
    client = RegisterApiClient(request_interval_seconds=request_interval)
    archive_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / "backfill-state.json"
    catalogue_path = output_root / "historical-source-registry.json"
    state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"completed_title_ids": []}
    completed = set(state.get("completed_title_ids", []))
    summaries: list[dict[str, Any]] = []
    if catalogue_path.exists():
        catalogue = json.loads(catalogue_path.read_text(encoding="utf-8"))
        sources = [SourceTitle(**entry) for entry in catalogue["sources"]]
    else:
        sources = historical_sources(client, instrument_limit)
        catalogue_path.write_text(
            json.dumps(
                {
                    "selection": "Migration Act 1958, Migration Regulations 1994, and principal Migration instruments authorised by either root, including former titles",
                    "sources": [source.__dict__ for source in sources],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    for source in sources:
        if source.title_id in completed and not reprocess_complete:
            summaries.append({"title_id": source.title_id, "status": "already-complete"})
            continue
        output = output_root / f"{source.title_id}.json"
        results = backfill_title(
            source.title_id,
            archive_root,
            output,
            source_kind=source.kind,
            from_date=from_date,
            to_date=to_date,
            skip_pdf=skip_pdf,
            neo4j=neo4j,
            request_interval=request_interval,
            client=client,
        )
        summaries.append({"title_id": source.title_id, "kind": source.kind, "versions": len(results), "status": "complete"})
        completed.add(source.title_id)
        state = {
            "completed_title_ids": sorted(completed),
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": to_date.isoformat() if to_date else None,
            "request_interval_seconds": request_interval,
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summaries[-1]), flush=True)
    return summaries


def ingest_regulations(as_at: date, archive_root: Path, output: Path, skip_pdf: bool = False, request_interval: float = 0.75) -> None:
    """Compatibility command retained for the focused Subclass 102 acceptance slice."""
    client = RegisterApiClient(request_interval_seconds=request_interval)
    result = ingest_title(client, RawArchive(archive_root), SourceTitle(MIGRATION_REGULATIONS_TITLE_ID, "Regulations"), as_at, include_pdf=not skip_pdf)
    result.graph.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.graph.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check_updates(as_at: date, archive_root: Path, include_instruments: bool, instrument_limit: int | None, request_interval: float = 0.75) -> list[dict[str, str | bool]]:
    client = RegisterApiClient(request_interval_seconds=request_interval)
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
    command.add_argument("--request-interval", type=float, default=0.75, help="minimum seconds between Register API requests")
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
    baseline.add_argument("--neo4j", action="store_true", help="upsert the validated graph using NEO4J_* environment variables")
    backfill = command.add_parser("backfill-title", help="archive and parse a title's available historical Register versions")
    backfill.add_argument("--title-id", required=True)
    backfill.add_argument("--kind", choices=("Act", "Regulations", "LegislativeInstrument"))
    backfill.add_argument("--from-date", type=_as_date)
    backfill.add_argument("--to-date", type=_as_date)
    backfill.add_argument("--version-limit", type=int)
    backfill.add_argument("--archive-root", type=Path, default=Path("data/raw"))
    backfill.add_argument("--output", type=Path, default=Path("data/backfill-graph.json"))
    backfill.add_argument("--skip-pdf", action="store_true")
    backfill.add_argument("--neo4j", action="store_true")
    backfill.add_argument("--request-interval", type=float, default=0.75)
    full_backfill = command.add_parser("backfill-baseline", help="resumably archive and parse the full historical Phase 1 source catalogue")
    full_backfill.add_argument("--from-date", type=_as_date)
    full_backfill.add_argument("--to-date", type=_as_date)
    full_backfill.add_argument("--instrument-limit", type=int, default=None, help="cap historical instrument titles for a bounded development run")
    full_backfill.add_argument("--archive-root", type=Path, default=Path("data/raw"))
    full_backfill.add_argument("--output-root", type=Path, default=Path("data/backfill"))
    full_backfill.add_argument("--skip-pdf", action="store_true")
    full_backfill.add_argument("--neo4j", action="store_true")
    full_backfill.add_argument("--request-interval", type=float, default=0.75)
    full_backfill.add_argument("--reprocess-complete", action="store_true", help="re-run completed titles while reusing immutable source files")
    updates = command.add_parser("check-updates", help="report versions not yet present in the immutable archive")
    _add_common_arguments(updates, include_instruments=True)
    args = parser.parse_args()
    if args.command == "ingest-regulations":
        ingest_regulations(args.as_at, args.archive_root, args.output, args.skip_pdf, args.request_interval)
    elif args.command == "ingest-baseline":
        results = ingest_baseline(args.as_at, args.archive_root, args.output, args.include_instruments, args.instrument_limit, args.skip_pdf, args.neo4j, args.request_interval)
        print(json.dumps({"ingested": len(results), "changed": sum(result.changed for result in results)}, indent=2))
    elif args.command == "check-updates":
        print(json.dumps(check_updates(args.as_at, args.archive_root, args.include_instruments, args.instrument_limit, args.request_interval), indent=2))
    elif args.command == "backfill-title":
        results = backfill_title(args.title_id, args.archive_root, args.output, args.kind, args.from_date, args.to_date, args.version_limit, args.skip_pdf, args.neo4j, args.request_interval)
        print(json.dumps({"ingested": len(results), "changed": sum(result.changed for result in results)}, indent=2))
    elif args.command == "backfill-baseline":
        summaries = backfill_baseline(args.archive_root, args.output_root, args.from_date, args.to_date, args.instrument_limit, args.skip_pdf, args.neo4j, args.request_interval, args.reprocess_complete)
        print(json.dumps({"titles": len(summaries), "completed": sum(item["status"] == "complete" for item in summaries)}, indent=2))


if __name__ == "__main__":
    main()
