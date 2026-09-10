"""Elle yazılmış tohum listesini veritabanına yükler."""

from __future__ import annotations

from pathlib import Path

import yaml

from .. import repo


def load_companies_yaml(path: str | Path) -> dict:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    source = data.get("source") or {}
    source_key = source.get("key", path.stem)
    repo.upsert_source(
        key=source_key,
        kind=source.get("kind", "manual"),
        name=source.get("name", path.name),
        url=source.get("url"),
        parser="seed.load_companies_yaml",
    )

    created = updated = 0
    for entry in data.get("companies") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        _, was_created = repo.upsert_company(
            name=name,
            website=entry.get("website"),
            city=entry.get("city"),
            source_key=source_key,
        )
        created += was_created
        updated += not was_created

    total = created + updated
    repo.mark_source_run(source_key, total, "ok" if total else "empty")
    return {"source": source_key, "created": created, "updated": updated, "total": total}
