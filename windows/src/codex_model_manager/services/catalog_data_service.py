"""High-level model/data service used by both GUI and CLI.

Equivalent to Sources/CodexModelManager/Services/CatalogDataService.swift
(macOS scheduling replaced by on-demand sync; backup-under-write preserved).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class CatalogSnapshot:
    models: List  # list[CatalogModel]
    records: List[dict]
    sync_log_path: str


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
        return CatalogSnapshot(models=models, records=records, sync_log_path=self.paths.sync_log)

    def add_custom_model(self, draft) -> None:
        from ..core.custom_models import add_from_template
        from ..parser import parse_models

        source_data = Path(self.paths.custom_source).read_bytes()
        merged_data = Path(self.paths.merged_catalog).read_bytes()
        models = parse_models(source_data, merged_data)
        updated = add_from_template(
            draft,
            source_data,
            catalog_data=merged_data,
            existing_slugs={m.slug for m in models},
        )
        self._write_custom_source(updated, source_data)

    def update_reasoning(self, settings, slug: str) -> None:
        from ..core.custom_models import update_reasoning

        source_data = Path(self.paths.custom_source).read_bytes()
        updated = update_reasoning(settings, slug, source_data)
        self._write_custom_source(updated, source_data)

    def _write_custom_source(self, updated_data: bytes, source_data: bytes) -> None:
        from ..core.backup import atomic_write, backup_file

        backup_file(self.paths.custom_source, self.paths.backup_directory, prefix="custom-models")
        atomic_write(self.paths.custom_source, updated_data)

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

        source_data = Path(path).read_bytes()
        updated = update_model(edit, slug, source_data, name=Path(path).name)
        if updated == source_data:
            return  # nothing actually changed: no backup, no write
        self._write_catalog(path, updated)

    def add_model_to(self, draft, path: str) -> None:
        """Add a model cloned from a template into `path` (backup + atomic write)."""
        from ..core.custom_models import add_from_template
        from ..parser import parse_catalog_models

        source_data = Path(path).read_bytes()
        existing = {m.slug for m in parse_catalog_models(source_data)}
        updated = add_from_template(
            draft, source_data, catalog_data=source_data, existing_slugs=existing)
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
