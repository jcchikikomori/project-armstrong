"""CLI: plan | apply | sidecar.

plan is the default and writes nothing. apply needs --yes on top, because the
target may be a droplet holding real records and there is no undo button.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .client import MediKeepClient
from .ledger import Ledger
from .mappers import map_everything
from .runner import apply, print_apply_summary, print_plan, say, write_sidecar
from .sources import load_all

PASSWORD_ENV = "MEDIKEEP_PASSWORD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medikeep-import",
        description="Load the Google Sheets health tracker CSVs into MediKeep.",
    )
    parser.add_argument(
        "command", nargs="?", default="plan", choices=("plan", "apply", "sidecar")
    )
    parser.add_argument("--datasets", type=Path, default=Path("/data"))
    parser.add_argument("--sidecar-dir", type=Path, default=None, help="default: <datasets>/derived")
    parser.add_argument("--ledger", type=Path, default=None, help="default: <sidecar-dir>/.import-ledger.sqlite3")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--patient-id", type=int, default=None)
    parser.add_argument(
        "--split-freetext",
        action="store_true",
        help="give every free-text complaint its own symptom instead of grouping them",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N creates; for smoke tests")
    parser.add_argument("--yes", action="store_true", help="required by apply")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sidecar_dir = args.sidecar_dir or args.datasets / "derived"
    ledger_path = args.ledger or sidecar_dir / ".import-ledger.sqlite3"

    if not args.datasets.is_dir():
        say(f"no datasets directory at {args.datasets}")
        return 1

    rows_by_file = load_all(args.datasets)
    loaded = {name: len(rows) for name, rows in rows_by_file.items() if rows}
    if not loaded:
        say(f"no CSVs found in {args.datasets}")
        return 1
    say("loaded " + ", ".join(f"{name}={count}" for name, count in sorted(loaded.items())))

    client: MediKeepClient | None = None
    patient_id = args.patient_id

    if args.command == "apply":
        if not args.yes:
            say("apply needs --yes; run `plan` first and read the warnings")
            return 1
        password = os.environ.get(PASSWORD_ENV)
        if not password:
            say(f"set {PASSWORD_ENV} in the environment (never on the command line)")
            return 1
        client = MediKeepClient(args.base_url)
        client.login(args.username, password)
        say(f"authenticated as {args.username} at {args.base_url}")
        if patient_id is None:
            patient_id = client.resolve_patient_id()
            say(f"resolved patient_id={patient_id}")

    # patient_id only ever lands in a payload, so a placeholder is harmless
    # for a dry run and keeps `plan` working with no network at all.
    result = map_everything(
        rows_by_file, patient_id if patient_id is not None else 1, split_freetext=args.split_freetext
    )

    if args.command == "sidecar":
        written = write_sidecar(result, sidecar_dir)
        for path in written:
            say(f"wrote {path}")
        return 0

    if args.command == "plan":
        if patient_id is None:
            say("dry run: payloads shown with a placeholder patient_id=1")
        try:
            with Ledger(ledger_path, args.base_url) as ledger:
                print_plan(result, ledger)
        except OSError as exc:
            # A dry run should survive a read-only datasets mount, which is
            # the sensible way to run one. Without the ledger it just cannot
            # say what was already imported.
            say(f"no ledger at {ledger_path} ({exc.strerror}); counts exclude prior runs")
            print_plan(result)
        return 0

    assert client is not None
    with Ledger(ledger_path, args.base_url) as ledger:
        stats = apply(result, client, ledger, limit=args.limit)
    print_apply_summary(stats)
    for path in write_sidecar(result, sidecar_dir):
        say(f"wrote {path}")
    return 1 if stats.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
