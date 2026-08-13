#!/usr/bin/env python3
"""Load the public oracle dataset and overlay local revised task JSON files.

This is intentionally a read-only construction step: it does not filter, write, or
publish a dataset. It prints the in-memory DatasetDict and an overlay summary.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from datasets import Dataset, DatasetDict, load_dataset
from tqdm import tqdm

from validate_tasks import runtime_checks, static_checks


DEFAULT_DATASET = "ServiceNow-AI/EnterpriseOps-Gym"
DEFAULT_CONFIG = "oracle"
REPO_ROOT = Path(__file__).resolve().parents[1]


def canonical_task_id(value: str) -> str:
    """Return the shared task ID used by hosted rows and local task filenames."""
    task_id = value.strip()
    if task_id.endswith(".json"):
        task_id = task_id[:-5]
    return task_id if task_id.startswith("task_") else f"task_{task_id}"


def load_revised_tasks(revised_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    tasks: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(revised_dir.rglob("task_*.json")):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f"Revised task must be a JSON object: {path}")
        tasks.append((path, payload))
    if not tasks:
        raise ValueError(f"No revised task JSON files found under {revised_dir}")
    return tasks


def encode_for_features(
    payload: dict[str, Any], task_id: str, domain: str, feature_names: set[str]
) -> dict[str, Any]:
    """Convert a local task JSON object to the hosted dataset row schema."""
    row = {key: value for key, value in payload.items() if key in feature_names}
    row["task_id"] = task_id
    if "domain" in feature_names:
        row["domain"] = domain

    # The public dataset stores these nested fields as JSON strings.
    for key in ("gym_servers_config", "verifiers"):
        if key in row and not isinstance(row[key], str):
            row[key] = json.dumps(row[key], sort_keys=True)

    # These fields are present in the public schema but absent from local task JSONs.
    if "restricted_tools" in feature_names:
        row.setdefault("restricted_tools", [])
    return row


def task_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Decode a hosted dataset row into the task shape used by the validator."""
    payload = dict(row)
    for key in ("gym_servers_config", "verifiers"):
        if isinstance(payload.get(key), str):
            payload[key] = json.loads(payload[key])
    return payload


def validate_merged_dataset(
    dataset: DatasetDict,
    runtime: bool,
    server_urls: dict[str, str],
) -> list[dict[str, Any]]:
    """Return one static/runtime validation record for every merged task."""
    records = []
    total_tasks = sum(len(split) for split in dataset.values())
    with tqdm(total=total_tasks, desc="Validating tasks", unit="task") as progress:
        for domain, split in dataset.items():
            progress.set_postfix_str(domain)
            for row in split:
                payload = task_payload(dict(row))
                task_id = canonical_task_id(payload["task_id"])
                issues = static_checks(Path(f"{domain}/{task_id}.json"), payload)
                if runtime and not issues:
                    issues.extend(runtime_checks(payload, server_urls))
                records.append(
                    {
                        "task_id": task_id,
                        "domain": domain,
                        "status": "validated" if not issues else "failed",
                        "issues": issues,
                    }
                )
                progress.update()
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="Hugging Face dataset ID.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Dataset configuration.")
    parser.add_argument(
        "--revision",
        default="main",
        help="Hugging Face revision to load (use a commit SHA for a pinned build).",
    )
    parser.add_argument(
        "--revised-dir",
        type=Path,
        default=REPO_ROOT / "data" / "revised",
        help="Directory containing corrected task JSON files.",
    )
    parser.add_argument(
        "--validate-static",
        action="store_true",
        help="Run static task/seed/verifier checks on the merged in-memory dataset.",
    )
    parser.add_argument(
        "--validate-runtime",
        action="store_true",
        help="Create disposable databases and validate MCP/server contracts for every task.",
    )
    parser.add_argument(
        "--server-url",
        action="append",
        default=[],
        metavar="MCP_NAME=URL",
        help="Override a task's MCP URL, e.g. sn-csm-server=http://localhost:8001.",
    )
    parser.add_argument(
        "--validation-report",
        type=Path,
        help="Write per-task validation JSONL here when validation is requested.",
    )
    parser.add_argument(
        "--filter-validated",
        action="store_true",
        help="Print only the in-memory task subset that passed the requested validation.",
    )
    args = parser.parse_args()
    if args.filter_validated and not (args.validate_static or args.validate_runtime):
        parser.error("--filter-validated requires --validate-static or --validate-runtime")
    if args.validate_runtime:
        args.validate_static = True
    server_urls = dict(item.split("=", 1) for item in args.server_url)

    source = load_dataset(args.dataset, args.config, revision=args.revision)
    if not isinstance(source, DatasetDict):
        raise TypeError("Expected a DatasetDict with one split per domain.")

    rows_by_id: dict[str, tuple[str, int]] = {}
    for domain, split in source.items():
        for index, row in enumerate(split):
            task_id = canonical_task_id(row["task_id"])
            if task_id in rows_by_id:
                raise ValueError(f"Duplicate hosted task ID: {task_id}")
            rows_by_id[task_id] = (domain, index)

    mutable_splits = {domain: [dict(row) for row in split] for domain, split in source.items()}
    overlay_actions: list[dict[str, str]] = []
    for path, payload in load_revised_tasks(args.revised_dir):
        task_id = canonical_task_id(str(payload.get("task_id", path.stem)))
        domain = path.parent.name
        if task_id in rows_by_id:
            hosted_domain, index = rows_by_id[task_id]
            if hosted_domain != domain:
                raise ValueError(
                    f"Domain mismatch for {task_id}: hosted={hosted_domain}, revised={domain}"
                )
            feature_names = set(source[domain].features)
            replacement = encode_for_features(payload, task_id, domain, feature_names)
            original = mutable_splits[domain][index]
            changed_fields = sorted(
                key for key in feature_names if original.get(key) != replacement.get(key)
            )
            mutable_splits[domain][index] = replacement
            action = "replaced"
        else:
            print(f"Skipping {task_id}, not in HF Dataset")
            continue
        overlay_actions.append(
            {
                "task_id": task_id,
                "action": action,
                "domain": domain,
                "source": str(path.relative_to(REPO_ROOT)),
                "changed_fields": ", ".join(changed_fields),
            }
        )

    merged = DatasetDict(
        {
            domain: Dataset.from_list(rows, features=source[domain].features)
            for domain, rows in mutable_splits.items()
        }
    )

    validation_records: list[dict[str, Any]] = []
    if args.validate_static:
        validation_records = validate_merged_dataset(
            merged, runtime=args.validate_runtime, server_urls=server_urls
        )
        if args.validation_report:
            args.validation_report.write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in validation_records)
            )
        counts = Counter(record["status"] for record in validation_records)
        issues = Counter(
            issue["kind"] for record in validation_records for issue in record["issues"]
        )
        print(f"Validation: {dict(counts)}; issues: {dict(sorted(issues.items()))}")
        if args.validation_report:
            print(f"Validation report: {args.validation_report}")

    if args.filter_validated:
        accepted = {record["task_id"] for record in validation_records if record["status"] == "validated"}
        merged = DatasetDict(
            {
                domain: split.filter(lambda row: canonical_task_id(row["task_id"]) in accepted)
                for domain, split in merged.items()
            }
        )
        print("\nValidated subset:")
        print(merged)
    else:
        print(merged)
    print(
        "\nLoaded "
        f"{sum(len(split) for split in source.values())} hosted tasks from "
        f"{args.dataset}@{args.revision}; applied {len(overlay_actions)} revised tasks."
    )
    print("Overlay actions:", dict(sorted(Counter(action["action"] for action in overlay_actions).items())))
    for action in overlay_actions:
        print(
            f"- {action['action']}: {action['task_id']} ({action['domain']}) "
            f"from {action['source']} [fields: {action['changed_fields']}]"
        )
        
    merged.push_to_hub("Brendan/EnterpriseOps-Gym")


if __name__ == "__main__":
    main()
