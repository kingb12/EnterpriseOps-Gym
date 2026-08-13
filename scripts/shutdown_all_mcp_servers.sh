#!/usr/bin/env bash
# Stop the named EnterpriseOps-Gym MCP containers without deleting them.
set -euo pipefail

domains=(csm teams calendar email itsm hr drive)

for domain in "${domains[@]}"; do
  container="enterpriseops-${domain}"
  if docker container inspect "$container" >/dev/null 2>&1; then
    docker stop "$container" >/dev/null
    echo "Stopped ${container}."
  fi
done

echo "EnterpriseOps-Gym MCP containers stopped; their Docker containers remain available to restart."
