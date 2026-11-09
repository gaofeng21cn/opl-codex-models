"""High-level model/data service used by both GUI and CLI.

Equivalent to Sources/CodexModelManager/Services/CatalogDataService.swift
(macOS scheduling replaced by on-demand sync; backup-under-write preserved).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import List

from ..core.errors import ConflictError


@contextmanager
def _catalog_write_lock(path):
    """Serialize GUI/CLI edits; keep the lock stable across atomic replacement."""
    target = Path(path).resolve()
    lock_path = target.with_name(f".{target.name}.lock")
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclass
class CatalogSnapshot:
    models: List  # list[CatalogModel]
    records: List[dict]
    sync_log_path: str
    source_digest: str = ""
    catalog_digest: str = ""
    custom_models: list | None = None


class CatalogDataService:
    def __init__(self, paths):
        self.paths = paths

    @classmethod
    def from_config(cls, config=None):
        from ..core.app_config import AppConfiguration

        config = config or AppConfiguration.load()
        return cls(config.resolved())

    def load_snapshot(self) -> CatalogSnapshot:
        from ..parser import parse_models, parse_records

        custom = Path(self.paths.custom_source).read_bytes()
        catalog = Path(self.paths.merged_catalog).read_bytes()
        models = parse_models(custom, catalog)
        log_text = ""
        if Path(self.paths.sync_log).exists():
            log_text = Path(self.paths.sync_log).read_text(encoding="utf-8", errors="replace")
        records = parse_records(log_text)
        return CatalogSnapshot(
            models=models, records=records, sync_log_path=self.paths.sync_log,
            source_digest=hashlib.sha256(custom).hexdigest(),
            catalog_digest=hashlib.sha256(catalog).hexdigest(),
            custom_models=json.loads(custom)["models"],
        )

    def add_custom_model(self, draft, *, expected_source_digest=None, dry_run=False):
        from ..core.custom_models import add_from_template
        from ..parser import parse_models

        def transform(source_data):
            merged_data = Path(self.paths.merged_catalog).read_bytes()
            models = parse_models(source_data, merged_data)
            custom_models = json.loads(source_data)["models"]
            return add_from_template(
                draft, source_data, catalog_data=merged_data,
                existing_slugs={m.slug for m in models} | {m["slug"] for m in custom_models},
            )
        return self._edit_custom_source(transform, expected_source_digest, dry_run)

    def update_reasoning(self, settings, slug: str, *, expected_source_digest=None, dry_run=False):
        from ..core.custom_models import update_reasoning

        return self._edit_custom_source(
            lambda source: update_reasoning(settings, slug, source), expected_source_digest, dry_run,
        )

    def remove_custom_model(self, slug: str, *, expected_source_digest=None, dry_run=False):
        from ..core.custom_models import remove_model

        return self._edit_custom_source(
            lambda source: remove_model(slug, source), expected_source_digest, dry_run,
        )

    def _edit_custom_source(self, transform, expected_source_digest, dry_run):
        def edit():
            source_data = Path(self.paths.custom_source).read_bytes()
            digest = hashlib.sha256(source_data).hexdigest()
            if expected_source_digest is not None and expected_source_digest != digest:
                raise ConflictError("模型源已变化；请重新读取 source_digest 后审阅操作。")
            updated = transform(source_data)
            changed = updated != source_data
            backup = None
            if not dry_run and changed:
                backup = self._write_custom_source(updated, source_data)
            return {
                "status": "preview" if dry_run else ("source_saved" if changed else "no_change"),
                "previous_source_digest": digest,
                "source_path": self.paths.custom_source,
                "source_digest": digest if dry_run else hashlib.sha256(updated).hexdigest(),
                "proposed_source_digest": hashlib.sha256(updated).hexdigest(),
                "custom_models": json.loads(updated)["models"],
                "backup_path": backup,
                "sync_required": not dry_run,
            }
        if dry_run:
            return edit()
        with _catalog_write_lock(self.paths.custom_source):
            return edit()

    def _write_custom_source(self, updated_data: bytes, source_data: bytes) -> None:
        from ..core.backup import atomic_write, backup_file

        if Path(self.paths.custom_source).read_bytes() != source_data:
            raise ConflictError("模型源在编辑期间变化；请重新读取后审阅操作。")
        backup = backup_file(self.paths.custom_source, self.paths.backup_directory, prefix="custom-models")
        atomic_write(self.paths.custom_source, updated_data)
        return backup

    # ---- takeover: edit the manager's pending copy of an existing catalog ----

    def load_catalog_snapshot(self, path: str, source: str = "pending") -> CatalogSnapshot:
        """Snapshot of one catalog file. The takeover view shows the pending copy,
        where every entry is editable regardless of how it started life."""
        from ..parser import parse_catalog_models

        data = Path(path).read_bytes()
        models = parse_catalog_models(data, source=source)
        return CatalogSnapshot(models=models, records=[], sync_log_path=self.paths.sync_log)

    def update_model_in(self, edit, slug: str, path: str) -> None:
        """Safe edit of one existing model in `path` (backup + atomic write)."""
        from ..core.custom_models import update_model

        with _catalog_write_lock(path):
            source_data = Path(path).read_bytes()
            updated = update_model(edit, slug, source_data, name=Path(path).name)
            if updated == source_data:
                return  # nothing actually changed: no backup, no write
            self._write_catalog(path, updated)

    def add_model_to(self, draft, path: str) -> None:
        """Add a model into `path` (backup + atomic write).

        With a source model the whole configuration is copied (unknown fields
        included); without one (first model in an empty catalog) a minimal entry is
        created from the fields the user supplied.
        """
        from ..core.custom_models import add_blank_model, add_from_template
        from ..parser import parse_catalog_models

        with _catalog_write_lock(path):
            source_data = Path(path).read_bytes()
            existing = {m.slug for m in parse_catalog_models(source_data)}
            if getattr(draft, "template_slug", ""):
                updated = add_from_template(
                    draft, source_data, catalog_data=source_data, existing_slugs=existing)
            else:
                updated = add_blank_model(draft, source_data, existing_slugs=existing)
            self._write_catalog(path, updated)

    def remove_model_in(self, slug: str, path: str) -> None:
        """Remove one model from `path` (backup + atomic write)."""
        from ..core.custom_models import remove_model

        with _catalog_write_lock(path):
            source_data = Path(path).read_bytes()
            updated = remove_model(slug, source_data, name=Path(path).name)
            self._write_catalog(path, updated)

    def _write_catalog(self, path: str, updated_data: bytes, prefix: str = "" ) -> None:
        from ..core.backup import atomic_write, backup_file

        backup_file(path, self.paths.backup_directory,
                    prefix=prefix or Path(path).stem or "catalog")
        atomic_write(path, updated_data)

    def run_sync(self):
        from ..core.catalog_sync import CatalogSyncService

        service = CatalogSyncService(self.paths)
        return service.sync_and_append_log()
