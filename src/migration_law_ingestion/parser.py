"""Deterministic EPUB parsing rules for the Migration Regulations structure."""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from zipfile import ZipFile

from .model import Graph, Node, Provenance, Relationship


PARSER_VERSION = "0.1.0"
SUBCLASS_RE = re.compile(r"^Subclass\s+(\d{3})\s*[—-]\s*(.+)$", re.I)
ITEM_RE = re.compile(r"^(\d{4}[A-Z]?)\s+(.+?\(Class\s+[A-Z]{2}\))$", re.I)
PROVISION_RE = re.compile(r"^(\d{3}\.\d+(?:[A-Z])?)$")
SCHEDULE_RE = re.compile(r"^Schedule(?:\s+([\dA-Z]+))?", re.I)
PART_RE = re.compile(r"^Part\s+([\dA-Z]+)", re.I)
DIVISION_RE = re.compile(r"^((?:Division\s+)?\d{3}\.\d+[A-Z]?)\s*[—-]\s*(.+)$", re.I)
SUBDIVISION_RE = re.compile(r"^(\d{3}\.\d{2}[A-Z]?)\s*[—-]\s*(.+)$", re.I)
PIC_RE = re.compile(r"public interest criteria?\s+([\d,\s and]+)", re.I)
SRC_RE = re.compile(r"special return criteria?\s+([\d,\s and]+)", re.I)
CONDITION_RE = re.compile(r"(?:visa )?conditions?\s+([\d,\s and]+)", re.I)
PROVISION_REFERENCE_RE = re.compile(r"(?:subclause|clause|regulation|section)\s+(\d{1,3}\.\d+(?:\(\d+\))?|\d+[A-Z]?)", re.I)
GENERIC_PROVISION_RE = re.compile(r"^(\d+[A-Z]?(?:\([A-Z0-9]+\))?)\b")
GENERIC_PART_RE = re.compile(r"^Part\s+([0-9A-Z]+)\s*[—-]\s*(.+)$", re.I)
GENERIC_DIVISION_RE = re.compile(r"^Division\s+([0-9A-Z]+)\s*[—-]\s*(.+)$", re.I)
GENERIC_SUBDIVISION_RE = re.compile(r"^Subdivision\s+([0-9A-Z]+)\s*[—-]\s*(.+)$", re.I)


@dataclass(frozen=True)
class Paragraph:
    document: str
    index: int
    css_class: str
    text: str


class _ParagraphExtractor(HTMLParser):
    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.document = document
        self.paragraphs: list[Paragraph] = []
        self._in_paragraph = False
        self._class = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "p":
            self._in_paragraph = True
            self._class = dict(attrs).get("class") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_paragraph:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "p" and self._in_paragraph:
            text = " ".join("".join(self._text).split())
            if text:
                self.paragraphs.append(Paragraph(self.document, len(self.paragraphs), self._class, text))
            self._in_paragraph = False


def extract_epub_paragraphs(epub: bytes) -> list[Paragraph]:
    """Read EPUB HTML in source-document order without browser rendering."""
    paragraphs: list[Paragraph] = []
    with ZipFile(BytesIO(epub)) as package:
        names = sorted(
            name for name in package.namelist() if name.startswith("OEBPS/") and name.endswith((".html", ".xhtml"))
        )
        for name in names:
            parser = _ParagraphExtractor(name)
            parser.feed(package.read(name).decode("utf-8", errors="replace"))
            offset = len(paragraphs)
            paragraphs.extend(Paragraph(p.document, p.index + offset, p.css_class, p.text) for p in parser.paragraphs)
    return paragraphs


@dataclass
class _Context:
    title_id: str
    version_id: str
    effective_from: str | None
    effective_to: str | None
    retrieved_at: str
    source_hash: str

    def provenance(self, paragraph: Paragraph | None, method: str, confidence: float = 1.0) -> Provenance:
        return Provenance(
            source_title_id=self.title_id,
            source_version_id=self.version_id,
            source_document=paragraph.document if paragraph else "register-api",
            source_location=f"{paragraph.document}#p-{paragraph.index}" if paragraph else "register-api",
            source_text=paragraph.text if paragraph else "",
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            retrieved_at=self.retrieved_at,
            source_hash=self.source_hash,
            parser_version=PARSER_VERSION,
            extraction_method=method,
            confidence=confidence,
        )


class RegulationsParser:
    """Rule-based Schedule 1 / Schedule 2 extractor; no ML or semantic guessing."""

    def parse_epub(
        self,
        epub: bytes,
        *,
        title_id: str,
        version_id: str,
        effective_from: str | None,
        effective_to: str | None,
        retrieved_at: str,
        source_hash: str,
    ) -> Graph:
        context = _Context(title_id, version_id, effective_from, effective_to, retrieved_at, source_hash)
        graph = Graph()
        version_node = f"version:{title_id}:{version_id}"
        self._node(graph, Node(version_node, ("LegislationVersion",), {"title_id": title_id, "register_id": version_id}, context.provenance(None, "register-version-json")))

        schedule_id: str | None = None
        subclass_id: str | None = None
        subclass_number: str | None = None
        current_provision_id: str | None = None
        current_item_id: str | None = None
        item_class_id: str | None = None
        part_id: str | None = None
        division_id: str | None = None
        subdivision_id: str | None = None

        for paragraph in extract_epub_paragraphs(epub):
            source = context.provenance(paragraph, "epub-html-structure-v1")
            schedule_match = SCHEDULE_RE.match(paragraph.text)
            if schedule_match and paragraph.css_class in {"ActHead1", "TOC1"}:
                number = schedule_match.group(1) or "unnumbered"
                candidate = f"schedule:{title_id}:{version_id}:{number}"
                # Table-of-contents nodes duplicate headings and have no legal text.
                if paragraph.css_class == "ActHead1":
                    schedule_id = candidate
                    self._node(graph, Node(schedule_id, ("Schedule",), {"number": number, "title": paragraph.text}, source))
                    self._rel(graph, "CONTAINS", version_node, schedule_id, source)
                    self._rel(graph, "PART_OF", schedule_id, version_node, source)
                    part_id = division_id = subdivision_id = None
                continue

            part_match = PART_RE.match(paragraph.text)
            if part_match and paragraph.css_class in {"ActHead2", "ActHead3"}:
                number = part_match.group(1)
                part_id = f"part:{title_id}:{version_id}:{schedule_id}:{number}"
                self._node(graph, Node(part_id, ("Part",), {"number": number, "title": paragraph.text}, source))
                if schedule_id:
                    self._rel(graph, "CONTAINS", schedule_id, part_id, source)
                    self._rel(graph, "PART_OF", part_id, schedule_id, source)
                division_id = subdivision_id = None
                continue

            subclass_match = SUBCLASS_RE.match(paragraph.text)
            if subclass_match and paragraph.css_class == "ActHead2":
                subclass_number, name = subclass_match.groups()
                subclass_id = f"visa-subclass:{title_id}:{version_id}:{subclass_number}"
                self._node(graph, Node(subclass_id, ("VisaSubclass",), {"number": subclass_number, "name": name}, source))
                if schedule_id:
                    self._rel(graph, "CONTAINS", schedule_id, subclass_id, source)
                    self._rel(graph, "PART_OF", subclass_id, schedule_id, source)
                current_provision_id = None
                division_id = subdivision_id = None
                continue

            division_match = DIVISION_RE.match(paragraph.text)
            if division_match and paragraph.css_class == "DivisionMigration":
                number, title = division_match.groups()
                division_id = f"division:{title_id}:{version_id}:{subclass_number or schedule_id}:{number}"
                parent = subclass_id or part_id or schedule_id
                self._node(graph, Node(division_id, ("Division",), {"number": number, "title": title}, source))
                if parent:
                    self._rel(graph, "CONTAINS", parent, division_id, source)
                    self._rel(graph, "PART_OF", division_id, parent, source)
                subdivision_id = None
                continue

            subdivision_match = SUBDIVISION_RE.match(paragraph.text)
            if subdivision_match and paragraph.css_class == "SubDivisionMigration":
                number, title = subdivision_match.groups()
                subdivision_id = f"subdivision:{title_id}:{version_id}:{subclass_number or schedule_id}:{number}"
                parent = division_id or subclass_id or part_id or schedule_id
                self._node(graph, Node(subdivision_id, ("Subdivision",), {"number": number, "title": title}, source))
                if parent:
                    self._rel(graph, "CONTAINS", parent, subdivision_id, source)
                    self._rel(graph, "PART_OF", subdivision_id, parent, source)
                continue

            item_match = ITEM_RE.match(paragraph.text)
            if item_match and paragraph.css_class == "ActHead5":
                item_number, class_name = item_match.groups()
                current_item_id = f"provision:{title_id}:{version_id}:item-{item_number}"
                item_class_id = f"visa-class:{title_id}:{version_id}:{item_number}"
                self._node(graph, Node(current_item_id, ("Provision",), {"kind": "schedule_1_item", "number": item_number, "title": paragraph.text}, source))
                self._node(graph, Node(item_class_id, ("VisaClass",), {"schedule_1_item": item_number, "name": class_name}, source))
                if schedule_id:
                    self._rel(graph, "CONTAINS", schedule_id, current_item_id, source)
                    self._rel(graph, "PART_OF", current_item_id, schedule_id, source)
                current_provision_id = current_item_id
                continue

            provision_match = PROVISION_RE.match(paragraph.text)
            if provision_match and paragraph.css_class == "ActHead5" and subclass_id:
                number = provision_match.group(1)
                current_provision_id = f"provision:{title_id}:{version_id}:{number}"
                self._node(graph, Node(current_provision_id, ("Provision",), {"kind": "schedule_2_clause", "number": number}, source))
                parent = subdivision_id or division_id or subclass_id
                if parent:
                    self._rel(graph, "CONTAINS", parent, current_provision_id, source)
                    self._rel(graph, "PART_OF", current_provision_id, parent, source)
                self._rel(graph, "HAS_GRANT_PROVISION", subclass_id, current_provision_id, source)
                continue

            # Schedule 1 item scope ends at the next Schedule 1 item; identify its listed subclasses.
            if current_item_id and item_class_id and paragraph.text == "Subclasses:":
                continue
            if current_item_id and item_class_id and re.fullmatch(r"\d{3}\s*\([^)]*\)", paragraph.text):
                number = paragraph.text.split()[0]
                target = f"visa-subclass:{title_id}:{version_id}:{number}"
                # This may be filled with detailed data later in Schedule 2; a reference node keeps the path intact now.
                if target not in graph.nodes:
                    self._node(graph, Node(target, ("VisaSubclass",), {"number": number, "name": None}, source))
                self._rel(graph, "HAS_SUBCLASS", item_class_id, target, source)
                self._rel(graph, "HAS_VALIDITY_PROVISION", target, current_item_id, source)

            if current_provision_id:
                self._extract_references(graph, current_provision_id, paragraph, context)

        return graph

    @staticmethod
    def _relationship_id(kind: str, start: str, end: str, provenance: Provenance) -> str:
        evidence = hashlib.sha256(f"{provenance.source_location}|{provenance.source_text}".encode()).hexdigest()[:12]
        return f"rel:{kind}:{start}:{end}:{evidence}"

    @staticmethod
    def _node(graph: Graph, node: Node) -> None:
        """Upgrade a placeholder reference when its full structural node appears."""
        existing = graph.nodes.get(node.id)
        if existing is None:
            graph.add_node(node)
            return
        properties = existing.properties | {key: value for key, value in node.properties.items() if value is not None}
        graph.nodes[node.id] = Node(node.id, tuple(sorted(set(existing.labels) | set(node.labels))), properties, node.provenance)

    def _rel(self, graph: Graph, kind: str, start: str, end: str, provenance: Provenance, properties: dict | None = None) -> None:
        graph.add_relationship(Relationship(self._relationship_id(kind, start, end, provenance), kind, start, end, properties or {}, provenance))

    def _extract_references(self, graph: Graph, provision_id: str, paragraph: Paragraph, context: _Context) -> None:
        source = context.provenance(paragraph, "epub-reference-regex-v1", 0.98)
        for pattern, label, kind, prefix in ((PIC_RE, "PIC", "REFERENCES_PIC", "pic"), (SRC_RE, "SRC", "REFERENCES_SRC", "src"), (CONDITION_RE, "VisaCondition", "REFERENCES_CONDITION", "condition")):
            match = pattern.search(paragraph.text)
            if not match:
                continue
            for number in re.findall(r"\b\d{4}\b", match.group(1)):
                target = f"{prefix}:{context.title_id}:{context.version_id}:{number}"
                self._node(graph, Node(target, (label,), {"number": number}, source))
                self._rel(graph, kind, provision_id, target, source)
        for reference in PROVISION_REFERENCE_RE.findall(paragraph.text):
            target = f"reference:{context.title_id}:{context.version_id}:{reference}"
            self._node(graph, Node(target, ("Provision",), {"citation": reference, "resolved": False}, source))
            self._rel(graph, "REFERENCES", provision_id, target, source)
        if "legislative instrument made" in paragraph.text.lower():
            target = f"instrument-hook:{context.title_id}:{context.version_id}:{provision_id}"
            self._node(graph, Node(target, ("LegislativeInstrument",), {"resolved": False, "hook_text": paragraph.text}, source))
            self._rel(graph, "SPECIFIED_BY", provision_id, target, source)


class GenericLegislationParser(RegulationsParser):
    """Extract ordinary Act/instrument structure from the Register EPUB layout."""

    def parse_epub(
        self,
        epub: bytes,
        *,
        title_id: str,
        version_id: str,
        effective_from: str | None,
        effective_to: str | None,
        retrieved_at: str,
        source_hash: str,
    ) -> Graph:
        context = _Context(title_id, version_id, effective_from, effective_to, retrieved_at, source_hash)
        graph = Graph()
        version_node = f"version:{title_id}:{version_id}"
        self._node(graph, Node(version_node, ("LegislationVersion",), {"title_id": title_id, "register_id": version_id}, context.provenance(None, "register-version-json")))
        schedule_id: str | None = None
        part_id: str | None = None
        division_id: str | None = None
        subdivision_id: str | None = None
        current_provision_id: str | None = None

        for paragraph in extract_epub_paragraphs(epub):
            source = context.provenance(paragraph, "epub-generic-structure-v1")
            if paragraph.text == "Endnotes" or paragraph.css_class.lower().startswith("endnote"):
                current_provision_id = None
                continue
            schedule_match = SCHEDULE_RE.match(paragraph.text)
            if schedule_match and paragraph.css_class == "ActHead1":
                number = schedule_match.group(1) or "unnumbered"
                schedule_id = f"schedule:{title_id}:{version_id}:{number}"
                self._node(graph, Node(schedule_id, ("Schedule",), {"number": number, "title": paragraph.text}, source))
                self._rel(graph, "CONTAINS", version_node, schedule_id, source)
                self._rel(graph, "PART_OF", schedule_id, version_node, source)
                part_id = division_id = subdivision_id = None
                current_provision_id = None
                continue
            part_match = GENERIC_PART_RE.match(paragraph.text)
            if part_match and paragraph.css_class == "ActHead2":
                number, title = part_match.groups()
                part_id = f"part:{title_id}:{version_id}:{number}"
                self._node(graph, Node(part_id, ("Part",), {"number": number, "title": title}, source))
                parent = schedule_id or version_node
                self._rel(graph, "CONTAINS", parent, part_id, source)
                self._rel(graph, "PART_OF", part_id, parent, source)
                division_id = subdivision_id = None
                current_provision_id = None
                continue
            division_match = GENERIC_DIVISION_RE.match(paragraph.text)
            if division_match and paragraph.css_class == "ActHead3":
                number, title = division_match.groups()
                division_id = f"division:{title_id}:{version_id}:{part_id or schedule_id}:{number}"
                self._node(graph, Node(division_id, ("Division",), {"number": number, "title": title}, source))
                parent = part_id or schedule_id or version_node
                self._rel(graph, "CONTAINS", parent, division_id, source)
                self._rel(graph, "PART_OF", division_id, parent, source)
                subdivision_id = None
                current_provision_id = None
                continue
            subdivision_match = GENERIC_SUBDIVISION_RE.match(paragraph.text)
            if subdivision_match and paragraph.css_class == "ActHead4":
                number, title = subdivision_match.groups()
                subdivision_id = f"subdivision:{title_id}:{version_id}:{division_id or part_id}:{number}"
                self._node(graph, Node(subdivision_id, ("Subdivision",), {"number": number, "title": title}, source))
                parent = division_id or part_id or schedule_id or version_node
                self._rel(graph, "CONTAINS", parent, subdivision_id, source)
                self._rel(graph, "PART_OF", subdivision_id, parent, source)
                current_provision_id = None
                continue
            provision_match = GENERIC_PROVISION_RE.match(paragraph.text)
            if provision_match and paragraph.css_class == "ActHead5":
                number = provision_match.group(1)
                parent = subdivision_id or division_id or part_id or schedule_id or version_node
                scope = hashlib.sha256(parent.encode()).hexdigest()[:12]
                current_provision_id = f"provision:{title_id}:{version_id}:{scope}:{number}"
                self._node(graph, Node(current_provision_id, ("Provision",), {"kind": "section_or_clause", "number": number, "title": paragraph.text, "structural_parent_id": parent}, source))
                self._rel(graph, "CONTAINS", parent, current_provision_id, source)
                self._rel(graph, "PART_OF", current_provision_id, parent, source)
                continue
            # Non-heading text belongs to the immediately preceding provision.
            if current_provision_id:
                self._extract_references(graph, current_provision_id, paragraph, context)
        return graph
