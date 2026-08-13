#!/usr/bin/env bash
# Pull, start, and health-check every EnterpriseOps-Gym single-domain MCP server.
set -euo pipefail

domains=(csm teams calendar email itsm hr drive)
host_ports=(8001 8002 8003 8004 8006 8008 8009)
container_ports=(8005 8005 8003 8005 8005 8005 8005)
image_prefix="shivakrishnareddyma225/enterpriseops-gym-mcp-"

for index in "${!domains[@]}"; do
  domain="${domains[$index]}"
  host_port="${host_ports[$index]}"
  container_port="${container_ports[$index]}"
  image="${image_prefix}${domain}:latest"
  container="enterpriseops-${domain}"

  if curl -fsS "http://localhost:${host_port}/health" >/dev/null 2>&1; then
    echo "${domain} already responds on localhost:${host_port}; leaving it unchanged."
    continue
  fi

  docker pull "$image"
  if docker container inspect "$container" >/dev/null 2>&1; then
    if ! docker port "$container" "$container_port" 2>/dev/null | grep -q ":${host_port}$"; then
      echo "${container} has an incorrect port mapping; recreating it."
      docker rm -f "$container" >/dev/null
      docker run -d --name "$container" -p "${host_port}:${container_port}" "$image" >/dev/null
      continue
    fi
    docker start "$container" >/dev/null 2>&1 || true
  else
    docker run -d --name "$container" -p "${host_port}:${container_port}" "$image" >/dev/null
  fi
done

for host_port in "${host_ports[@]}"; do
  for attempt in {1..30}; do
    if curl -fsS "http://localhost:${host_port}/health" >/dev/null; then
      break
    fi
    if [[ "$attempt" == 30 ]]; then
      echo "Server on localhost:${host_port} did not become healthy." >&2
      exit 1
    fi
    sleep 1
  done
done

docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
echo "All EnterpriseOps-Gym MCP servers are healthy."
