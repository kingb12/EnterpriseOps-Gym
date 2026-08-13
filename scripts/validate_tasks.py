#!/usr/bin/env python3
"""Validate EnterpriseOps-Gym task contracts without invoking an agent.

Static checks run by default. Add ``--runtime`` to seed disposable databases, verify
MCP tool exposure, run database-state verifier SQL, and delete every created database.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TASK_FIELDS = {
    "system_prompt",
    "user_prompt",
    "gym_servers_config",
    "selected_tools",
    "verifiers",
}


def context_headers(context: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in context.items():
        name = key if key.lower().startswith("x-") else f"x-{key.replace('_', '-')}"
        headers[name] = str(value)
    return headers


def load_tasks(tasks_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    tasks = []
    for path in sorted(tasks_dir.rglob("task_*.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            tasks.append((path, {"_load_error": str(error)}))
            continue
        tasks.append((path, payload))
    if not tasks:
        raise ValueError(f"No task JSON files found under {tasks_dir}")
    return tasks


def static_checks(path: Path, task: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if "_load_error" in task:
        return [{"kind": "invalid_json", "detail": task["_load_error"]}]
    missing = sorted(REQUIRED_TASK_FIELDS - task.keys())
    if missing:
        issues.append({"kind": "invalid_schema", "detail": f"missing fields: {missing}"})
    gyms = task.get("gym_servers_config")
    if not isinstance(gyms, list) or not gyms:
        issues.append({"kind": "invalid_schema", "detail": "gym_servers_config must be a non-empty list"})
        return issues
    if not isinstance(task.get("selected_tools"), list):
        issues.append({"kind": "invalid_schema", "detail": "selected_tools must be a list"})
    if not isinstance(task.get("verifiers"), list):
        issues.append({"kind": "invalid_schema", "detail": "verifiers must be a list"})
    for gym in gyms:
        if not isinstance(gym, dict):
            issues.append({"kind": "invalid_schema", "detail": "gym server must be an object"})
            continue
        for field in ("mcp_server_name", "mcp_server_url", "seed_database_file"):
            if not gym.get(field):
                issues.append({"kind": "invalid_schema", "detail": f"gym missing {field}"})
        seed = Path(gym.get("seed_database_file", ""))
        if gym.get("seed_database_file") and not seed.is_file():
            issues.append({"kind": "invalid_seed", "detail": str(seed)})
    for verifier in task.get("verifiers", []):
        if not isinstance(verifier, dict) or not verifier.get("verifier_type"):
            issues.append({"kind": "invalid_schema", "detail": "verifier missing verifier_type"})
            continue
        if verifier["verifier_type"] == "database_state":
            query = verifier.get("validation_config", {}).get("query")
            if not isinstance(query, str) or not query.strip():
                issues.append({"kind": "invalid_verifier_sql", "detail": "database_state verifier has no query"})
    return issues


def create_database(url: str, seed: Path) -> str:
    database_id = f"validate_{int(time.time() * 1000)}_" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=8)
    )
    payload = {
        "database_id": database_id,
        "name": f"Validation {database_id}",
        "description": "Disposable no-agent validation database",
        "sql_content": seed.read_text(),
    }
    with httpx.Client(timeout=max(120, len(payload["sql_content"]) / 10000)) as client:
        response = client.post(f"{url.rstrip('/')}/api/seed-database", json=payload)
        response.raise_for_status()
    return database_id


def delete_database(url: str, database_id: str) -> None:
    with httpx.Client(timeout=30) as client:
        response = client.request(
            "DELETE", f"{url.rstrip('/')}/api/delete-database", json={"database_id": database_id}
        )
        response.raise_for_status()


def mcp_tools(url: str, database_id: str, context: dict[str, Any]) -> set[str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers.update(context_headers(context))
    headers["x-database-id"] = database_id
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "clientInfo": {"name": "task-validator", "version": "1"}},
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(f"{url.rstrip('/')}/mcp", json=initialize, headers=headers)
        response.raise_for_status()
        session = response.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        response = client.post(
            f"{url.rstrip('/')}/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        response.raise_for_status()
    payload = response.json()
    return {tool["name"] for tool in payload.get("result", {}).get("tools", [])}


def validate_sql(url: str, database_id: str, context: dict[str, Any], query: str) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "x-database-id": database_id}
    headers.update(context_headers(context))
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{url.rstrip('/')}/api/sql-runner",
            json={"query": query, "database_id": database_id},
            headers=headers,
        )
        response.raise_for_status()
    return response.json()


def runtime_checks(task: dict[str, Any], server_urls: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for gym in task["gym_servers_config"]:
        name = gym["mcp_server_name"]
        url = server_urls.get(name, gym["mcp_server_url"])
        seed = Path(gym["seed_database_file"])
        database_id = ""
        try:
            with httpx.Client(timeout=10) as client:
                health = client.get(f"{url.rstrip('/')}/health")
                health.raise_for_status()
            database_id = create_database(url, seed)
            exposed = mcp_tools(url, database_id, gym.get("context") or {})
            missing_tools = sorted(set(task.get("selected_tools", [])) - exposed)
            if missing_tools:
                issues.append({"kind": "missing_selected_tool", "detail": f"{name}: {missing_tools}"})
            for verifier in task.get("verifiers", []):
                if verifier.get("verifier_type") != "database_state":
                    continue
                if verifier.get("gym_name") not in (None, name):
                    continue
                try:
                    validate_sql(url, database_id, gym.get("context") or {}, verifier["validation_config"]["query"])
                except Exception as error:  # Keep validating the remaining verifiers.
                    issues.append({"kind": "invalid_verifier_sql", "detail": f"{name}: {error}"})
        except Exception as error:
            issues.append({"kind": "server_contract_failure", "detail": f"{name}: {error}"})
        finally:
            if database_id:
                try:
                    delete_database(url, database_id)
                except Exception as error:
                    issues.append({"kind": "cleanup_failure", "detail": f"{name}/{database_id}: {error}"})
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, default=REPO_ROOT / "data" / "revised")
    parser.add_argument("--runtime", action="store_true", help="Run server/database/MCP checks.")
    parser.add_argument(
        "--server-url",
        action="append",
        default=[],
        metavar="MCP_NAME=URL",
        help="Override a task's MCP URL, e.g. sn-csm-server=http://localhost:8001.",
    )
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "validation_report.jsonl")
    args = parser.parse_args()
    server_urls = dict(item.split("=", 1) for item in args.server_url)

    records = []
    for path, task in load_tasks(args.tasks_dir):
        issues = static_checks(path, task)
        if args.runtime and not issues:
            issues.extend(runtime_checks(task, server_urls))
        records.append(
            {
                "task_id": path.stem,
                "path": str(path.resolve().relative_to(REPO_ROOT)),
                "status": "validated" if not issues else "failed",
                "issues": issues,
            }
        )

    args.report.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
    counts = Counter(record["status"] for record in records)
    issue_counts = Counter(issue["kind"] for record in records for issue in record["issues"])
    print(f"Validated {len(records)} tasks: {dict(counts)}")
    print(f"Issue counts: {dict(sorted(issue_counts.items()))}")
    print(f"Report: {args.report}")


if __name__ == "__main__":
    main()
