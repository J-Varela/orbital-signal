"""Bounded local checkpoints for search pages awaiting transaction enrichment."""

import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import suppress
from datetime import date
from pathlib import Path
from time import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orbital_signal.domain import AwardRecord


class SourceCacheError(Exception):
    """A pending-page checkpoint could not be read, saved, or removed."""


class PendingSearchPage(BaseModel):
    """Validated source observations; failed lookups never enter action_dates."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    agency: str
    start_date: date
    end_date: date
    page: int = Field(ge=1)
    records_per_page: int = Field(ge=1, le=100)
    awards: list[AwardRecord] = Field(max_length=100)
    has_more: bool
    action_dates: dict[str, date | None] = Field(default_factory=dict, max_length=100)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    last_accessed_at: float = Field(ge=0, allow_inf_nan=False)


class PendingEnrichmentCache:
    """Store only pending pages, with atomic private files and a fixed page budget."""

    def __init__(
        self, *, directory: Path, base_url: str, ttl_seconds: float = 86400, max_pages: int = 8
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("enrichment cache TTL must be finite and positive")
        if max_pages < 1:
            raise ValueError("enrichment cache page budget must be positive")
        self.directory = directory
        self.origin_digest = hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()
        self.ttl_seconds = ttl_seconds
        self.max_pages = max_pages

    def _key(
        self, *, agency: str, start_date: date, end_date: date, page: int, records_per_page: int
    ) -> str:
        identity = json.dumps(
            [
                self.origin_digest,
                agency,
                start_date.isoformat(),
                end_date.isoformat(),
                page,
                records_per_page,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    def _remove(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise SourceCacheError("Unable to remove a pending enrichment checkpoint") from exc

    def _read(self, path: Path, *, now: float) -> PendingSearchPage | None:
        try:
            if path.is_symlink():
                self._remove(path)
                return None
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError:
            self._remove(path)
            return None
        except OSError as exc:
            raise SourceCacheError("Unable to read a pending enrichment checkpoint") from exc
        try:
            entry = PendingSearchPage.model_validate_json(content)
        except (ValidationError, ValueError, UnicodeError):
            self._remove(path)
            return None
        if (
            entry.key_digest != path.stem
            or entry.created_at > now
            or now - entry.created_at >= self.ttl_seconds
            or entry.end_date < entry.start_date
            or not set(entry.action_dates).issubset(
                award.generated_internal_id for award in entry.awards
            )
        ):
            self._remove(path)
            return None
        expected = hashlib.sha256(
            json.dumps(
                [
                    entry.origin_digest,
                    entry.agency,
                    entry.start_date.isoformat(),
                    entry.end_date.isoformat(),
                    entry.page,
                    entry.records_per_page,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if entry.key_digest != expected:
            self._remove(path)
            return None
        return entry

    def _prune(self, *, now: float) -> dict[str, PendingSearchPage]:
        try:
            paths = list(self.directory.iterdir())
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SourceCacheError(
                "Unable to inspect the pending enrichment cache directory"
            ) from exc
        entries = {}
        for path in paths:
            if re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                entry = self._read(path, now=now)
                if entry is not None:
                    entries[entry.key_digest] = entry
            elif path.name.startswith(".pending-") and path.suffix == ".tmp":
                try:
                    if now - path.stat().st_mtime >= self.ttl_seconds:
                        self._remove(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise SourceCacheError(
                        "Unable to inspect a pending cache temporary file"
                    ) from exc
        ordered = sorted(
            entries.values(), key=lambda entry: (entry.last_accessed_at, entry.key_digest)
        )
        for entry in ordered[: max(0, len(ordered) - self.max_pages)]:
            self._remove(self.directory / f"{entry.key_digest}.json")
            del entries[entry.key_digest]
        return entries

    def _write(self, entry: PendingSearchPage) -> None:
        temporary = None
        try:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(
                prefix=".pending-", suffix=".tmp", dir=self.directory
            )
            temporary = Path(filename)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(entry.model_dump_json())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.directory / f"{entry.key_digest}.json")
        except OSError as exc:
            raise SourceCacheError("Unable to save a pending enrichment checkpoint") from exc
        finally:
            if temporary is not None:
                # Preserve the original write failure if cleanup also fails.
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def get(
        self, *, agency: str, start_date: date, end_date: date, page: int, records_per_page: int
    ) -> PendingSearchPage | None:
        now = time()
        key = self._key(
            agency=agency,
            start_date=start_date,
            end_date=end_date,
            page=page,
            records_per_page=records_per_page,
        )
        entry = self._prune(now=now).get(key)
        if entry is not None:
            entry.last_accessed_at = now
            self._write(entry)
        return entry

    def new_page(
        self,
        *,
        agency: str,
        start_date: date,
        end_date: date,
        page: int,
        records_per_page: int,
        awards: list[AwardRecord],
        has_more: bool,
    ) -> PendingSearchPage:
        now = time()
        return PendingSearchPage(
            key_digest=self._key(
                agency=agency,
                start_date=start_date,
                end_date=end_date,
                page=page,
                records_per_page=records_per_page,
            ),
            origin_digest=self.origin_digest,
            agency=agency,
            start_date=start_date,
            end_date=end_date,
            page=page,
            records_per_page=records_per_page,
            awards=awards,
            has_more=has_more,
            created_at=now,
            last_accessed_at=now,
        )

    def save(self, entry: PendingSearchPage) -> None:
        entry.last_accessed_at = time()
        self._write(entry)
        self._prune(now=entry.last_accessed_at)

    def discard(self, entry: PendingSearchPage) -> None:
        self._remove(self.directory / f"{entry.key_digest}.json")

    def invalidate(self, *, start_date: date, end_date: date, agencies: tuple[str, ...]) -> None:
        for entry in self._prune(now=time()).values():
            if (
                entry.origin_digest == self.origin_digest
                and entry.agency in agencies
                and entry.start_date <= end_date
                and entry.end_date >= start_date
            ):
                self.discard(entry)
