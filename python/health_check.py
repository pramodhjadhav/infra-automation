#!/usr/bin/env python3
"""
health_check.py
---------------
CLI application that checks the health of the two infra-lab servers
(infra-web1 / infra-web2) provisioned by Terraform and configured by Ansible.

Checks performed per server
  1. SSH connectivity
  2. CPU usage       (via `top -bn1`)
  3. Memory usage    (via `free -m`)
  4. Disk usage      (via `df -h /`)
  5. Nginx service   (via `systemctl is-active nginx`)
  6. HTTP home page  (GET http://127.0.0.1:<port>/)
  7. HTTP /health    (GET http://127.0.0.1:<port>/health)

Usage
  python health_check.py                      # all servers, table output
  python health_check.py --server web1        # single server
  python health_check.py --output json        # JSON report
  python health_check.py --config servers.yml # custom config file

Exit codes
  0  all servers healthy
  1  one or more servers degraded / unreachable
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import paramiko
import requests
from tabulate import tabulate

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "health_check.log"

_json_fmt = logging.Formatter(
    '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}'
)
_console_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

logger = logging.getLogger("health_check")
logger.setLevel(logging.DEBUG)

# Console handler – INFO and above
_ch = logging.StreamHandler(sys.stderr)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_console_fmt)
logger.addHandler(_ch)

# Rotating file handler – DEBUG and above (structured JSON)
try:
    _fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3
    )
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(_json_fmt)
    logger.addHandler(_fh)
except OSError:
    # If the log file cannot be created (e.g. read-only filesystem) keep going.
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

STATUS_OK   = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

OVERALL_HEALTHY     = "HEALTHY"
OVERALL_DEGRADED    = "DEGRADED"
OVERALL_UNREACHABLE = "UNREACHABLE"

THRESHOLD_PERCENT = 90  # CPU / memory / disk warn threshold


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str       # OK | FAIL | SKIP
    value: str = ""   # human-readable measured value (e.g. "14.2%")
    message: str = "" # short explanation when not OK


@dataclasses.dataclass
class ServerConfig:
    name: str
    host: str
    ssh_port: int
    http_port: int
    ssh_user: str
    ssh_key_path: str


@dataclasses.dataclass
class ServerReport:
    server: str
    checks: list[CheckResult]
    overall: str
    elapsed_s: float


# ──────────────────────────────────────────────────────────────────────────────
# Default server configurations (mirrors terraform/variables.tf defaults)
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_KEY = os.path.expanduser("~/.ssh/infra_lab")

DEFAULT_SERVERS: list[ServerConfig] = [
    ServerConfig(
        name="web1",
        host="127.0.0.1",
        ssh_port=2221,
        http_port=8081,
        ssh_user="devops",
        ssh_key_path=_DEFAULT_KEY,
    ),
    ServerConfig(
        name="web2",
        host="127.0.0.1",
        ssh_port=2222,
        http_port=8082,
        ssh_user="devops",
        ssh_key_path=_DEFAULT_KEY,
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# SSH helper
# ──────────────────────────────────────────────────────────────────────────────

SSH_TIMEOUT = 10  # seconds


class SSHSession:
    """Thin wrapper around paramiko.SSHClient used as a context manager."""

    def __init__(self, cfg: ServerConfig) -> None:
        self._cfg = cfg
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self._cfg.host,
                port=self._cfg.ssh_port,
                username=self._cfg.ssh_user,
                key_filename=self._cfg.ssh_key_path,
                timeout=SSH_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception:
            client.close()
            raise
        self._client = client

    def run(self, command: str) -> str:
        """Execute *command* and return stdout (stripped). Raises on SSH error."""
        if self._client is None:
            raise RuntimeError("SSHSession not connected")
        _, stdout, stderr = self._client.exec_command(command, timeout=SSH_TIMEOUT)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        logger.debug(
            '"cmd":"%s","rc":%d,"stdout":"%s","stderr":"%s"',
            command, exit_code, out[:200], err[:200],
        )
        return out

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHSession":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ──────────────────────────────────────────────────────────────────────────────
# Individual check functions
# ──────────────────────────────────────────────────────────────────────────────

def check_ssh(cfg: ServerConfig) -> tuple[CheckResult, SSHSession | None]:
    """
    Try to open an SSH connection to the server.
    Returns (CheckResult, SSHSession) so subsequent checks can reuse the session.
    """
    session = SSHSession(cfg)
    try:
        session.connect()
        logger.debug('"check":"ssh","server":"%s","status":"OK"', cfg.name)
        return CheckResult(name="SSH", status=STATUS_OK, value="connected"), session
    except paramiko.AuthenticationException as exc:
        logger.warning('"check":"ssh","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="SSH", status=STATUS_FAIL, message="auth failed"), None
    except (paramiko.SSHException, OSError) as exc:
        logger.warning('"check":"ssh","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="SSH", status=STATUS_FAIL, message=str(exc)[:80]), None


def check_cpu(session: SSHSession, server_name: str) -> CheckResult:
    """Parse CPU idle% from `top -bn1` output and derive usage%."""
    try:
        out = session.run("top -bn1 | grep -i 'Cpu(s)'")
        # Example: "%Cpu(s):  5.6 us,  1.2 sy,  0.0 ni, 92.3 id, ..."
        match = re.search(r"(\d+\.\d+)\s+id", out)
        if not match:
            return CheckResult(name="CPU", status=STATUS_FAIL, message="cannot parse top output")
        idle = float(match.group(1))
        usage = round(100.0 - idle, 1)
        status = STATUS_OK if usage < THRESHOLD_PERCENT else STATUS_FAIL
        logger.debug('"check":"cpu","server":"%s","usage":%.1f', server_name, usage)
        return CheckResult(name="CPU", status=status, value=f"{usage}%",
                           message="" if status == STATUS_OK else f"usage {usage}% >= {THRESHOLD_PERCENT}%")
    except Exception as exc:
        logger.warning('"check":"cpu","server":"%s","error":"%s"', server_name, exc)
        return CheckResult(name="CPU", status=STATUS_FAIL, message=str(exc)[:80])


def check_memory(session: SSHSession, server_name: str) -> CheckResult:
    """Parse memory usage% from `free -m` output."""
    try:
        out = session.run("free -m")
        # Example line: "Mem:           3832        1456        1832          78         543        2219"
        for line in out.splitlines():
            if line.lower().startswith("mem:"):
                parts = line.split()
                total = int(parts[1])
                used  = int(parts[2])
                if total == 0:
                    return CheckResult(name="MEM", status=STATUS_FAIL, message="total=0")
                usage = round(used / total * 100, 1)
                status = STATUS_OK if usage < THRESHOLD_PERCENT else STATUS_FAIL
                logger.debug('"check":"mem","server":"%s","usage":%.1f', server_name, usage)
                return CheckResult(name="MEM", status=status, value=f"{usage}%",
                                   message="" if status == STATUS_OK else f"usage {usage}% >= {THRESHOLD_PERCENT}%")
        return CheckResult(name="MEM", status=STATUS_FAIL, message="Mem: line not found")
    except Exception as exc:
        logger.warning('"check":"mem","server":"%s","error":"%s"', server_name, exc)
        return CheckResult(name="MEM", status=STATUS_FAIL, message=str(exc)[:80])


def check_disk(session: SSHSession, server_name: str) -> CheckResult:
    """Parse root filesystem usage% from `df -h /`."""
    try:
        out = session.run("df -h /")
        # Skip header; last line is the / entry.
        # Example: "/dev/mapper/... 20G 3.6G 16G 19% /"
        lines = [l for l in out.splitlines() if l and not l.startswith("Filesystem")]
        if not lines:
            return CheckResult(name="DISK", status=STATUS_FAIL, message="no df output")
        parts = lines[-1].split()
        # The Use% column is the 5th field (index 4), always ends with %
        use_field = next((p for p in parts if p.endswith("%")), None)
        if use_field is None:
            return CheckResult(name="DISK", status=STATUS_FAIL, message="cannot parse df output")
        usage = int(use_field.rstrip("%"))
        status = STATUS_OK if usage < THRESHOLD_PERCENT else STATUS_FAIL
        logger.debug('"check":"disk","server":"%s","usage":%d', server_name, usage)
        return CheckResult(name="DISK", status=status, value=f"{usage}%",
                           message="" if status == STATUS_OK else f"usage {usage}% >= {THRESHOLD_PERCENT}%")
    except Exception as exc:
        logger.warning('"check":"disk","server":"%s","error":"%s"', server_name, exc)
        return CheckResult(name="DISK", status=STATUS_FAIL, message=str(exc)[:80])


def check_nginx_service(session: SSHSession, server_name: str) -> CheckResult:
    """Check that the nginx systemd unit is active."""
    try:
        out = session.run("systemctl is-active nginx")
        status = STATUS_OK if out == "active" else STATUS_FAIL
        logger.debug('"check":"nginx_svc","server":"%s","state":"%s"', server_name, out)
        return CheckResult(name="NGINX", status=status, value=out,
                           message="" if status == STATUS_OK else f"state={out!r}")
    except Exception as exc:
        logger.warning('"check":"nginx_svc","server":"%s","error":"%s"', server_name, exc)
        return CheckResult(name="NGINX", status=STATUS_FAIL, message=str(exc)[:80])


def check_http_home(cfg: ServerConfig) -> CheckResult:
    """GET http://127.0.0.1:<port>/ and expect HTTP 200."""
    url = f"http://{cfg.host}:{cfg.http_port}/"
    try:
        resp = requests.get(url, timeout=5)
        status = STATUS_OK if resp.status_code == 200 else STATUS_FAIL
        logger.debug('"check":"http_home","server":"%s","code":%d', cfg.name, resp.status_code)
        return CheckResult(name="HTTP", status=status, value=str(resp.status_code),
                           message="" if status == STATUS_OK else f"HTTP {resp.status_code}")
    except requests.exceptions.ConnectionError as exc:
        logger.warning('"check":"http_home","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="HTTP", status=STATUS_FAIL, message="connection refused")
    except requests.exceptions.Timeout:
        logger.warning('"check":"http_home","server":"%s","error":"timeout"', cfg.name)
        return CheckResult(name="HTTP", status=STATUS_FAIL, message="timeout")
    except requests.exceptions.RequestException as exc:
        logger.warning('"check":"http_home","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="HTTP", status=STATUS_FAIL, message=str(exc)[:80])


def check_http_health(cfg: ServerConfig) -> CheckResult:
    """GET http://127.0.0.1:<port>/health and expect HTTP 200 with body 'ok'."""
    url = f"http://{cfg.host}:{cfg.http_port}/health"
    try:
        resp = requests.get(url, timeout=5)
        body = resp.text.strip()
        if resp.status_code == 200 and body == "ok":
            logger.debug('"check":"http_health","server":"%s","status":"OK"', cfg.name)
            return CheckResult(name="/health", status=STATUS_OK, value="ok")
        msg = f"HTTP {resp.status_code}, body={body!r}"
        logger.warning('"check":"http_health","server":"%s","detail":"%s"', cfg.name, msg)
        return CheckResult(name="/health", status=STATUS_FAIL, message=msg)
    except requests.exceptions.ConnectionError as exc:
        logger.warning('"check":"http_health","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="/health", status=STATUS_FAIL, message="connection refused")
    except requests.exceptions.Timeout:
        logger.warning('"check":"http_health","server":"%s","error":"timeout"', cfg.name)
        return CheckResult(name="/health", status=STATUS_FAIL, message="timeout")
    except requests.exceptions.RequestException as exc:
        logger.warning('"check":"http_health","server":"%s","error":"%s"', cfg.name, exc)
        return CheckResult(name="/health", status=STATUS_FAIL, message=str(exc)[:80])


# ──────────────────────────────────────────────────────────────────────────────
# Per-server orchestration
# ──────────────────────────────────────────────────────────────────────────────

def run_checks(cfg: ServerConfig) -> ServerReport:
    """Run all seven checks for *cfg* and return a ServerReport."""
    logger.info('"event":"start","server":"%s"', cfg.name)
    t0 = time.monotonic()
    checks: list[CheckResult] = []

    # 1. SSH — if this fails all SSH-based checks are skipped
    ssh_result, session = check_ssh(cfg)
    checks.append(ssh_result)

    if session is not None:
        with session:
            checks.append(check_cpu(session, cfg.name))
            checks.append(check_memory(session, cfg.name))
            checks.append(check_disk(session, cfg.name))
            checks.append(check_nginx_service(session, cfg.name))
    else:
        for name in ("CPU", "MEM", "DISK", "NGINX"):
            checks.append(CheckResult(name=name, status=STATUS_SKIP, message="SSH failed"))

    # 6 & 7. HTTP checks are independent of SSH
    checks.append(check_http_home(cfg))
    checks.append(check_http_health(cfg))

    # Overall status
    statuses = {c.status for c in checks}
    if ssh_result.status == STATUS_FAIL:
        overall = OVERALL_UNREACHABLE
    elif STATUS_FAIL in statuses:
        overall = OVERALL_DEGRADED
    else:
        overall = OVERALL_HEALTHY

    elapsed = round(time.monotonic() - t0, 2)
    logger.info('"event":"done","server":"%s","overall":"%s","elapsed_s":%.2f',
                cfg.name, overall, elapsed)
    return ServerReport(server=cfg.name, checks=checks, overall=overall, elapsed_s=elapsed)


# ──────────────────────────────────────────────────────────────────────────────
# Report formatters
# ──────────────────────────────────────────────────────────────────────────────

# Ordered list of check names that appear as columns in the table
_CHECK_COLS = ["SSH", "CPU", "MEM", "DISK", "NGINX", "HTTP", "/health"]

_STATUS_SYMBOL = {
    STATUS_OK:   "OK",
    STATUS_FAIL: "FAIL",
    STATUS_SKIP: "—",
}

_OVERALL_SYMBOL = {
    OVERALL_HEALTHY:     "HEALTHY",
    OVERALL_DEGRADED:    "DEGRADED",
    OVERALL_UNREACHABLE: "UNREACHABLE",
}


def _report_as_table(reports: list[ServerReport]) -> str:
    headers = ["Server"] + _CHECK_COLS + ["Overall", "Time(s)"]
    rows = []
    for r in reports:
        check_map = {c.name: c for c in r.checks}
        row = [r.server]
        for col in _CHECK_COLS:
            c = check_map.get(col)
            if c is None:
                row.append("—")
            elif c.status == STATUS_OK:
                row.append(c.value if c.value else STATUS_OK)
            elif c.status == STATUS_SKIP:
                row.append("—")
            else:
                row.append(f"FAIL({c.message})" if c.message else "FAIL")
        row.append(_OVERALL_SYMBOL[r.overall])
        row.append(f"{r.elapsed_s:.2f}s")
        rows.append(row)
    return tabulate(rows, headers=headers, tablefmt="simple")


def _report_as_json(reports: list[ServerReport]) -> str:
    output = []
    for r in reports:
        output.append({
            "server":  r.server,
            "overall": r.overall,
            "elapsed_s": r.elapsed_s,
            "checks": [
                {
                    "name":    c.name,
                    "status":  c.status,
                    "value":   c.value,
                    "message": c.message,
                }
                for c in r.checks
            ],
        })
    return json.dumps(output, indent=2)


# ──────────────────────────────────────────────────────────────────────────────
# Config file loader (optional YAML)
# ──────────────────────────────────────────────────────────────────────────────

def load_servers_from_yaml(path: str) -> list[ServerConfig]:
    """
    Load server definitions from a YAML file.  Expected format:

      servers:
        - name: web1
          host: 127.0.0.1
          ssh_port: 2221
          http_port: 8081
          ssh_user: devops
          ssh_key_path: ~/.ssh/infra_lab
        - name: web2
          ...
    """
    try:
        import yaml  # optional dependency; only needed with --config
    except ImportError:
        sys.exit("PyYAML is not installed. Run: pip install pyyaml")

    with open(path) as fh:
        data = yaml.safe_load(fh)

    servers = []
    for s in data.get("servers", []):
        servers.append(ServerConfig(
            name=s["name"],
            host=s.get("host", "127.0.0.1"),
            ssh_port=int(s["ssh_port"]),
            http_port=int(s["http_port"]),
            ssh_user=s.get("ssh_user", "devops"),
            ssh_key_path=os.path.expanduser(s.get("ssh_key_path", "~/.ssh/infra_lab")),
        ))
    return servers


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="health_check",
        description="Infrastructure health checker for the infra-automation lab.",
    )
    p.add_argument(
        "--server",
        default="all",
        metavar="NAME",
        help="Server to check: web1 | web2 | all  (default: all)",
    )
    p.add_argument(
        "--output",
        choices=["table", "json"],
        default="table",
        help="Output format  (default: table)",
    )
    p.add_argument(
        "--config",
        metavar="FILE",
        help="Path to a YAML file that overrides the default server list",
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        dest="log_level",
        help="Console log level  (default: INFO)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Adjust console log level
    _ch.setLevel(getattr(logging, args.log_level))

    # Resolve server list
    if args.config:
        all_servers = load_servers_from_yaml(args.config)
    else:
        all_servers = DEFAULT_SERVERS

    if args.server == "all":
        servers = all_servers
    else:
        servers = [s for s in all_servers if s.name == args.server]
        if not servers:
            names = ", ".join(s.name for s in all_servers)
            sys.exit(f"Unknown server '{args.server}'. Available: {names}")

    # Run checks
    print(f"\nInfra Health Report  —  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)
    reports = [run_checks(cfg) for cfg in servers]
    print()

    # Print report
    if args.output == "json":
        print(_report_as_json(reports))
    else:
        print(_report_as_table(reports))

    # Summary line
    print()
    all_healthy = all(r.overall == OVERALL_HEALTHY for r in reports)
    if all_healthy:
        print("All servers healthy.")
    else:
        unhealthy = [r.server for r in reports if r.overall != OVERALL_HEALTHY]
        print(f"WARNING: {', '.join(unhealthy)} not healthy.")

    return 0 if all_healthy else 1


if __name__ == "__main__":
    sys.exit(main())
