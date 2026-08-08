"""Small, dependency-free client for the Federal Register of Legislation API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
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

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_seconds: int = 60, request_interval_seconds: float = 0.75, max_retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def _wait_for_slot(self) -> None:
        delay = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
        if delay > 0:
            time.sleep(delay)

    def _get(self, path: str, accept: str) -> tuple[bytes, dict[str, str], str]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(url, headers={"Accept": accept, "User-Agent": "migration-law-ingestion/0.1"})
        for attempt in range(self.max_retries + 1):
            self._wait_for_slot()
            self._last_request_at = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: fixed government API base
                    return response.read(), dict(response.headers.items()), url
            except HTTPError as error:
                retryable = error.code in {429, 500, 502, 503, 504}
                if not retryable or attempt == self.max_retries:
                    raise
                retry_after = error.headers.get("Retry-After") if error.headers else None
                backoff = float(retry_after) if retry_after and retry_after.isdigit() else max(self.request_interval_seconds, 2**attempt)
                time.sleep(backoff)
            except URLError:
                if attempt == self.max_retries:
                    raise
                time.sleep(max(self.request_interval_seconds, 2**attempt))
        raise RuntimeError("unreachable")

    def get_json(self, path: str) -> tuple[dict[str, Any], str]:
        raw, _, url = self._get(path, "application/json")
        return json.loads(raw), url

    def get_title(self, title_id: str, include_authority: bool = False) -> tuple[dict[str, Any], str]:
        suffix = "?%24expand=AuthorisedBy" if include_authority else ""
        return self.get_json(f"Titles('{quote(title_id, safe='')}'){suffix}")

    def list_migration_titles(
        self,
        in_force_only: bool = True,
        page_size: int = 100,
    ) -> tuple[list[dict[str, Any]], str]:
        """Return every Register title whose name contains ``Migration``.

        Register pagination is followed explicitly.  Historical backfill uses the
        non-current catalogue so a repealed principal instrument is not silently
        lost just because it is no longer in force today.
        """
        filters = ["contains(name, 'Migration')"]
        if in_force_only:
            filters.append("isInForce eq true")
        filter_expression = " and ".join(filters)
        titles: list[dict[str, Any]] = []
        skip = 0
        first_url = ""
        expected_count: int | None = None
        while True:
            query = urlencode({"$filter": filter_expression, "$top": page_size, "$skip": skip})
            payload, url = self.get_json(f"Titles?{query}")
            first_url = first_url or url
            if expected_count is None and isinstance(payload.get("@odata.count"), int):
                expected_count = payload["@odata.count"]
            page = list(payload.get("value", []))
            titles.extend(page)
            if not page or len(page) < page_size or (expected_count is not None and len(titles) >= expected_count):
                return titles, first_url
            skip += page_size

    def list_in_force_migration_titles(self) -> tuple[list[dict[str, Any]], str]:
        """Compatibility wrapper for the current, daily-update source catalogue."""
        return self.list_migration_titles(in_force_only=True)

    def get_version(self, title_id: str, as_at: date) -> tuple[dict[str, Any], str]:
        return self.get_json(f"versions/find(titleid='{quote(title_id, safe='')}',asat={as_at.isoformat()})")

    def get_version_by_register_id(self, register_id: str) -> tuple[dict[str, Any], str]:
        return self.get_json(f"versions/find(registerid='{quote(register_id, safe='')}')")

    def list_versions(self, title_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        """List every available point-in-time version for an explicit backfill."""
        versions: list[dict[str, Any]] = []
        skip = 0
        while True:
            query = urlencode({"$filter": f"titleId eq '{title_id}'", "$top": page_size, "$skip": skip})
            payload, _ = self.get_json(f"Versions?{query}")
            page = list(payload.get("value", []))
            versions.extend(page)
            if len(page) < page_size:
                return versions
            skip += page_size

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

    def download_primary_document_by_register_id(
        self,
        register_id: str,
        document_format: str,
        volume_number: int = 0,
        unique_type_number: int = 0,
    ) -> DownloadedDocument:
        path = (
            "documents/find("
            f"registerId='{quote(register_id, safe='')}',"
            "type='Primary',"
            f"format='{document_format}',"
            f"uniqueTypeNumber={unique_type_number},volumeNumber={volume_number},"
            "rectificationSpecification='Latest')"
        )
        body, headers, url = self._get(path, "application/octet-stream")
        disposition = headers.get("Content-Disposition", "")
        filename = next((part.split("=", 1)[1].strip().strip('"') for part in disposition.split(";") if part.strip().startswith("filename=")), None)
        return DownloadedDocument(body, headers.get("Content-Type"), filename, url)
