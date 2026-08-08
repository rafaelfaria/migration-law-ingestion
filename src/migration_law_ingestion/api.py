"""Small, dependency-free client for the Federal Register of Legislation API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://api.prod.legislation.gov.au/v1"


@dataclass(frozen=True)
class DownloadedDocument:
    body: bytes
    content_type: str | None
    filename: str | None
    request_url: str


class RegisterApiClient:
    """Calls documented API routes; it never falls back to Register web pages."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_seconds: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, accept: str) -> tuple[bytes, dict[str, str], str]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(url, headers={"Accept": accept, "User-Agent": "migration-law-ingestion/0.1"})
        with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: fixed government API base
            return response.read(), dict(response.headers.items()), url

    def get_json(self, path: str) -> tuple[dict[str, Any], str]:
        raw, _, url = self._get(path, "application/json")
        return json.loads(raw), url

    def get_title(self, title_id: str, include_authority: bool = False) -> tuple[dict[str, Any], str]:
        suffix = "?%24expand=AuthorisedBy" if include_authority else ""
        return self.get_json(f"Titles('{quote(title_id, safe='')}'){suffix}")

    def list_in_force_migration_titles(self) -> tuple[list[dict[str, Any]], str]:
        """Return Register candidates using a documented OData query, not web search."""
        query = urlencode({"$filter": "isInForce eq true and contains(name, 'Migration')"})
        payload, url = self.get_json(f"Titles?{query}")
        return list(payload.get("value", [])), url

    def get_version(self, title_id: str, as_at: date) -> tuple[dict[str, Any], str]:
        return self.get_json(f"versions/find(titleid='{quote(title_id, safe='')}',asat={as_at.isoformat()})")

    def download_primary_document(
        self,
        title_id: str,
        as_at: date,
        document_format: str,
        volume_number: int = 0,
        unique_type_number: int = 0,
    ) -> DownloadedDocument:
        """Download a current rectification of the primary document for a version."""
        path = (
            "documents/find("
            f"titleid='{quote(title_id, safe='')}',asat={as_at.isoformat()},"
            "type='Primary',"
            f"format='{document_format}',"
            f"uniqueTypeNumber={unique_type_number},volumeNumber={volume_number},"
            "rectificationSpecification='Latest')"
        )
        body, headers, url = self._get(path, "application/octet-stream")
        disposition = headers.get("Content-Disposition", "")
        filename = None
        for part in disposition.split(";"):
            if part.strip().startswith("filename="):
                filename = part.split("=", 1)[1].strip().strip('"')
                break
        return DownloadedDocument(body, headers.get("Content-Type"), filename, url)
