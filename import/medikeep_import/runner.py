"""Turning mapped Records into HTTP calls, and reporting what happened.

Output is prefixed with the tool name and written to stdout, the same shape
deploy/scripts/export-json.sh uses, so a cron log stays readable.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .client import MediKeepClient, MediKeepError
from .ledger import Ledger
from .models import MappingResult, Record

PREFIX = "medikeep-import:"


def say(message: str) -> None:
    print(f"{PREFIX} {message}")


def summarize(result: MappingResult) -> Counter:
    return Counter(record.entity for record in result.records)


def print_plan(result: MappingResult, ledger: Ledger | None = None) -> None:
    counts = summarize(result)
    already = ledger.counts() if ledger else {}
    say(f"{len(result.records)} records to create across {len(counts)} entity types")
    for entity, count in sorted(counts.items()):
        seen = already.get(entity, 0)
        suffix = f"  ({seen} already in the ledger)" if seen else ""
        print(f"  {entity:<22} {count:>5}{suffix}")

    if result.sidecar:
        say("sidecar (written as JSON, never sent to MediKeep)")
        for name, rows in sorted(result.sidecar.items()):
            print(f"  {name:<22} {len(rows):>5}")

    if result.warnings:
        say(f"{len(result.warnings)} warnings")
        for warning in result.warnings:
            print(f"  [{warning.kind}] {warning.source_key}: {warning.detail}")


def write_sidecar(result: MappingResult, out_dir: Path) -> list[Path]:
    """Write the derived JSON, then lock it down.

    These are medical records in plaintext, so they get the same 0600 that
    export-json.sh gives its output.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    written = []
    for name, rows in sorted(result.sidecar.items()):
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
        written.append(path)
    return written


def apply(
    result: MappingResult,
    client: MediKeepClient,
    ledger: Ledger,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """POST every record that is not already in the ledger.

    A 4xx on one record does not stop the run. With a few hundred rows, one
    rejected payload should cost that row, not the other 299 -- and the
    failure list at the end is the thing worth reading.
    """
    stats = Counter()
    created = 0
    for record in result.records:
        if limit is not None and created >= limit:
            stats["stopped_at_limit"] += 1
            continue
        if ledger.remote_id(record.source_key) is not None:
            stats["skipped"] += 1
            continue

        path = record.path
        payload = record.payload
        if record.parent_key is not None:
            parent_id = ledger.remote_id(record.parent_key)
            if parent_id is None:
                say(f"skipping {record.source_key}: parent {record.parent_key} was not created")
                stats["orphaned"] += 1
                continue
            path = path.format(parent_id=parent_id)
            if record.parent_field:
                payload = {**payload, record.parent_field: parent_id}

        try:
            response = client.post(path, payload)
        except MediKeepError as exc:
            say(f"FAILED {record.source_key} -> {exc}")
            stats["failed"] += 1
            continue

        remote_id = response.get("id")
        if not isinstance(remote_id, int):
            say(f"WARNING {record.source_key}: created but no id in the response")
            stats["created_without_id"] += 1
            continue

        ledger.record(record.source_key, record.entity, remote_id)
        stats[f"created:{record.entity}"] += 1
        created += 1

    return dict(stats)


def print_apply_summary(stats: dict[str, int]) -> None:
    created = {k.split(":", 1)[1]: v for k, v in stats.items() if k.startswith("created:")}
    total = sum(created.values())
    say(f"created {total} records")
    for entity, count in sorted(created.items()):
        print(f"  {entity:<22} {count:>5}")
    for key in ("skipped", "failed", "orphaned", "created_without_id", "stopped_at_limit"):
        if stats.get(key):
            print(f"  {key:<22} {stats[key]:>5}")


def records_by_entity(result: MappingResult, entity: str) -> list[Record]:
    return [r for r in result.records if r.entity == entity]
