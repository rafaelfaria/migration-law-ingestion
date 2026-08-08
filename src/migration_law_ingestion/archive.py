"""Immutable content-addressed filesystem archive for Register source material."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArchivedObject:
    path: Path
    sha256: str
    size_bytes: int
    retrieved_at: str
    request_url: str | None = None
    content_type: str | None = None
    reused: bool = False


class RawArchive:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _retrieved_at() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def store_bytes(
        self,
        title_id: str,
        version_id: str,
        filename: str,
        content: bytes,
        *,
        request_url: str | None = None,
        content_type: str | None = None,
    ) -> ArchivedObject:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / title_id / version_id / digest / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        reused = target.exists()
        if reused:
            existing = target.read_bytes()
            if existing != content:
                raise RuntimeError(f"immutable archive collision at {target}")
        else:
            target.write_bytes(content)
        return ArchivedObject(target, digest, len(content), self._retrieved_at(), request_url, content_type, reused)

    def store_json(
        self, title_id: str, version_id: str, kind: str, payload: dict[str, Any], request_url: str
    ) -> ArchivedObject:
        # Retrieval context is in the immutable manifest. Keeping response bytes
        # canonical means an unchanged API response has the same archive address.
        content = self.json_bytes(payload)
        return self.store_bytes(title_id, version_id, f"{kind}.json", content, request_url=request_url, content_type="application/json")

    @staticmethod
    def json_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    def has_json_payload(self, title_id: str, version_id: str, kind: str, payload: dict[str, Any]) -> bool:
        digest = hashlib.sha256(self.json_bytes(payload)).hexdigest()
        return (self.root / title_id / version_id / digest / f"{kind}.json").exists()

    def latest_document(self, title_id: str, version_id: str, suffix: str) -> Path | None:
        """Return an archived source document, preferring the newest object path."""
        base = self.root / title_id / version_id
        candidates = sorted(base.glob(f"*/*{suffix}"))
        return candidates[-1] if candidates else None

    def previous_version(self, title_id: str, current_version_id: str, current_start: str | None) -> tuple[dict[str, Any], Path] | None:
        """Find the latest earlier archived version using its Register interval."""
        base = self.root / title_id
        candidates: list[tuple[str, dict[str, Any], Path]] = []
        if not base.exists():
            return None
        for version_dir in base.iterdir():
            if not version_dir.is_dir() or version_dir.name == current_version_id:
                continue
            metadata_files = sorted(version_dir.glob("*/version.json"))
            if not metadata_files:
                continue
            metadata_path = metadata_files[-1]
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            start = payload.get("start") or ""
            if current_start and start >= current_start:
                continue
            candidates.append((start, payload, metadata_path))
        if not candidates:
            return None
        _, payload, path = max(candidates, key=lambda candidate: candidate[0])
        return payload, path

    def write_manifest(self, title_id: str, version_id: str, entries: list[ArchivedObject]) -> Path:
        path = self.root / title_id / version_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"title_id": title_id, "version_id": version_id, "objects": [asdict(e) | {"path": str(e.path)} for e in entries]}
        encoded = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if path.exists() and path.read_bytes() != encoded:
            # A manifest is a snapshot, so append retrieval snapshots rather than alter it.
            path = path.with_name(f"manifest-{self._retrieved_at().replace(':', '')}.json")
        path.write_bytes(encoded)
        return path
