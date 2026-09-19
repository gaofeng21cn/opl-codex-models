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

    def run_sync(self):
        from ..core.catalog_sync import CatalogSyncService

        service = CatalogSyncService(self.paths)
        return service.sync_and_append_log()