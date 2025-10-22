#!/usr/bin/env python3
"""
@file homelab_agent.py
@brief Background agent to collect homelab machine metrics and POST to homelab-db.

Reads configuration from environment variables (optionally .env file):
  - HOMELAB_DB_BASE_URL (e.g., http://127.0.0.1:8000)
  - HOMELAB_DB_API_PREFIX (default: /api/v1)           # change if you alter API prefix
  - HOMELAB_DB_ENDPOINT  (default: /metrics/)          # the collection endpoint path
  - SERVER_NAME           (e.g., "proxmox-01")
  - POST_INTERVAL_SECONDS (default: 30)
  - PROCESS_LIMIT         (default: 40)                # max processes to include
  - API_TOKEN             (optional, adds Authorization: Bearer <token>)

Sends payload shaped for homelab-db's POST /api/v1/metrics/ endpoint:
{
  "server_name": str,
  "cpu_usage": int,                 # 0..100
  "memory_usage": int,              # 0..100
  "disk_space_used": int,           # bytes (total used across mounted partitions)
  "network_traffic_in": int,        # bytes received since boot (cumulative)
  "network_traffic_out": int,       # bytes sent since boot (cumulative)
  "uptime": int,                    # seconds since boot
  "status": "running",
  "running_processes": "[...]"      # JSON string (list of process names)
}

@repo
  https://github.com/msle237-lees/homelab-db  (see README → API Endpoints)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import Dict, List, Set

import psutil
import requests

# Optional: load .env if present
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

BASE_URL = os.getenv("HOMELAB_DB_BASE_URL", "http://cluster-1-pi5:8000").rstrip("/")
API_PREFIX = os.getenv("HOMELAB_DB_API_PREFIX", "/api/v1").rstrip("/")
ENDPOINT = os.getenv("HOMELAB_DB_ENDPOINT", "/metrics/")
SERVER_NAME = os.getenv("SERVER_NAME", os.uname().nodename if hasattr(os, "uname") else "unknown")
POST_INTERVAL = int(os.getenv("POST_INTERVAL_SECONDS", "30"))
PROCESS_LIMIT = int(os.getenv("PROCESS_LIMIT", "40"))
API_TOKEN = os.getenv("API_TOKEN")

POST_URL = f"{BASE_URL}{API_PREFIX}{ENDPOINT}"

_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def _unique_mountpoints() -> Set[str]:
    mounts: Set[str] = set()
    for p in psutil.disk_partitions(all=False):
        # Skip pseudo / special filesystems
        if any(p.fstype.lower().startswith(x) for x in ("proc", "sysfs", "devfs", "tmpfs", "devtmpfs", "overlay")):
            continue
        # Skip unreadable mountpoints
        if not os.access(p.mountpoint, os.R_OK):
            continue
        mounts.add(p.mountpoint)
    # Always include root as a fallback
    mounts.add("/")
    return mounts


def _disk_used_bytes() -> int:
    total_used = 0
    seen: Set[str] = set()
    for m in _unique_mountpoints():
        try:
            if m in seen:
                continue
            du = psutil.disk_usage(m)
            total_used += int(du.used)
            seen.add(m)
        except Exception:
            continue
    return total_used


def _running_process_names(limit: int) -> List[str]:
    names: List[str] = []
    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            name = proc.info.get("name") or (proc.info.get("cmdline") or ["unknown"])[0]
            if not name:
                name = "unknown"
            names.append(str(name))
            if len(names) >= limit:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


def _collect_metrics() -> Dict:
    # CPU: 0..100 (psutil returns float, we clamp to int)
    # Using a short interval to get an instantaneous-ish reading without blocking too long.
    cpu_usage = int(round(psutil.cpu_percent(interval=0.3)))

    # Memory usage percent
    mem = psutil.virtual_memory()
    memory_usage = int(round(mem.percent))

    # Disk used bytes (summed across readable partitions)
    disk_space_used = _disk_used_bytes()

    # Network cumulative (since boot)
    net = psutil.net_io_counters()
    network_in = int(net.bytes_recv)
    network_out = int(net.bytes_sent)

    # Uptime in seconds
    boot_time = psutil.boot_time()
    uptime = int(time.time() - boot_time)

    # Running processes (JSON string)
    procs = _running_process_names(PROCESS_LIMIT)
    running_processes_json = json.dumps(procs, ensure_ascii=False)

    payload = {
        "server_name": SERVER_NAME,
        "cpu_usage": max(0, min(cpu_usage, 100)),
        "memory_usage": max(0, min(memory_usage, 100)),
        "disk_space_used": max(0, disk_space_used),
        "network_traffic_in": max(0, network_in),
        "network_traffic_out": max(0, network_out),
        "uptime": max(0, uptime),
        "status": "running",
        "running_processes": running_processes_json,
    }
    return payload


def _post_loop():
    session = requests.Session()
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    backoff = 1.0  # seconds, exponential backoff on errors (capped)
    backoff_max = 60.0

    while not _shutdown:
        payload = _collect_metrics()
        try:
            resp = session.post(POST_URL, headers=headers, json=payload, timeout=10)
            if resp.status_code // 100 == 2:
                # Success → reset backoff
                backoff = 1.0
            else:
                # Log HTTP error (print to journal/syslog via systemd capture)
                print(f"[homelab-agent] POST {POST_URL} -> {resp.status_code} {resp.text[:200]}", file=sys.stderr)
                # gentle backoff
                time.sleep(backoff)
                backoff = min(backoff * 2.0, backoff_max)
        except requests.RequestException as e:
            print(f"[homelab-agent] POST error: {e}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, backoff_max)

        # Sleep the normal interval unless we're shutting down
        for _ in range(POST_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)


def main():
    # Graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _handle_shutdown)

    print(f"[homelab-agent] starting; posting to {POST_URL} as {SERVER_NAME} every {POST_INTERVAL}s")
    _post_loop()
    print("[homelab-agent] stopped.")


if __name__ == "__main__":
    main()
