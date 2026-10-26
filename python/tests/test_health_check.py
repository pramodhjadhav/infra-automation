"""
test_health_check.py
--------------------
Unit tests for health_check.py.

All tests use unittest.mock so they run without live infrastructure.
The test suite covers:

  Test 1 – check_ssh returns FAIL when paramiko raises AuthenticationException
  Test 2 – check_cpu correctly extracts usage% from a known `top` output string
  Test 3 – check_memory correctly computes used% from a known `free -m` string
  Test 4 – check_http_health returns FAIL when requests.get raises ConnectionError
  Test 5 – overall status is DEGRADED when one check is FAIL and the rest are OK

Run with:
  pytest python/tests/ -v          (from the repo root)
  pytest tests/ -v                 (from python/)
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Allow `import health_check` regardless of where pytest is invoked from.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import health_check as hc


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _make_cfg(name="web2", ssh_port=2221, http_port=8081) -> hc.ServerConfig:
    return hc.ServerConfig(
        name=name,
        host="127.0.0.1",
        ssh_port=ssh_port,
        http_port=http_port,
        ssh_user="devops",
        ssh_key_path="/home/user/.ssh/infra_lab",
    )


def _mock_session(stdout_lines: dict[str, str]) -> MagicMock:
    """Return a mock SSHSession whose run() returns preset values per command pattern."""
    session = MagicMock(spec=hc.SSHSession)

    def _run_side_effect(cmd: str) -> str:
        for key, val in stdout_lines.items():
            if key in cmd:
                return val
        return ""

    session.run.side_effect = _run_side_effect
    return session


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 – SSH check returns FAIL when authentication fails
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckSSH:
    """check_ssh must return STATUS_FAIL when paramiko raises AuthenticationException."""

    def test_auth_failure_returns_fail(self):
        import paramiko

        cfg = _make_cfg()

        with patch.object(hc.SSHSession, "connect",
                          side_effect=paramiko.AuthenticationException("bad key")):
            result, session = hc.check_ssh(cfg)

        assert result.status == hc.STATUS_FAIL, (
            "Expected STATUS_FAIL on AuthenticationException, got %s" % result.status
        )
        assert session is None, "Session must be None when SSH fails"

    def test_connection_error_returns_fail(self):
        """An unreachable host should also produce STATUS_FAIL."""
        cfg = _make_cfg()

        with patch.object(hc.SSHSession, "connect",
                          side_effect=OSError("Connection refused")):
            result, session = hc.check_ssh(cfg)

        assert result.status == hc.STATUS_FAIL
        assert session is None

    def test_successful_connect_returns_ok(self):
        """A successful connect must return STATUS_OK and a live session."""
        cfg = _make_cfg()

        with patch.object(hc.SSHSession, "connect", return_value=None):
            result, session = hc.check_ssh(cfg)

        assert result.status == hc.STATUS_OK
        assert session is not None


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 – CPU parser extracts the correct usage% from a real `top` line
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckCPU:
    """check_cpu must correctly parse idle% and derive usage%."""

    # Typical `top -bn1 | grep Cpu` output on a lightly loaded Fedora container
    TOP_OUTPUT_LOW = "%Cpu(s):  5.6 us,  1.2 sy,  0.0 ni, 92.3 id,  0.0 wa,  0.5 hi,  0.4 si,  0.0 st"
    TOP_OUTPUT_HIGH = "%Cpu(s): 88.0 us,  5.0 sy,  0.0 ni,  6.0 id,  0.0 wa,  0.5 hi,  0.5 si,  0.0 st"

    def test_low_cpu_is_ok(self):
        session = _mock_session({"top": self.TOP_OUTPUT_LOW})
        result = hc.check_cpu(session, "web1")

        assert result.status == hc.STATUS_OK, "Expected OK for low CPU usage"
        # idle=92.3 → usage=7.7
        assert result.value == "7.7%", f"Unexpected value: {result.value!r}"

    def test_high_cpu_is_fail(self):
        session = _mock_session({"top": self.TOP_OUTPUT_HIGH})
        result = hc.check_cpu(session, "web1")

        # idle=6.0 → usage=94.0 which is >= THRESHOLD_PERCENT (90)
        assert result.status == hc.STATUS_FAIL, "Expected FAIL for CPU >= 90%"

    def test_unparseable_top_output_is_fail(self):
        session = _mock_session({"top": "garbled output with no idle field"})
        result = hc.check_cpu(session, "web1")

        assert result.status == hc.STATUS_FAIL
        assert "parse" in result.message.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Test 3 – Memory parser computes used% from a real `free -m` block
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckMemory:
    """check_memory must correctly compute used/total %."""

    FREE_OUTPUT_LOW = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:            3832        1456        1832          78         543        2219\n"
        "Swap:           2047           0        2047"
    )
    FREE_OUTPUT_HIGH = (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:            3832        3600         150          30          82          82\n"
        "Swap:           2047           0        2047"
    )

    def test_low_memory_is_ok(self):
        session = _mock_session({"free": self.FREE_OUTPUT_LOW})
        result = hc.check_memory(session, "web1")

        assert result.status == hc.STATUS_OK
        # used=1456, total=3832 → 38.0%
        assert result.value == "38.0%", f"Unexpected value: {result.value!r}"

    def test_high_memory_is_fail(self):
        session = _mock_session({"free": self.FREE_OUTPUT_HIGH})
        result = hc.check_memory(session, "web1")

        # used=3600, total=3832 → 93.9% which is >= 90
        assert result.status == hc.STATUS_FAIL

    def test_missing_mem_line_is_fail(self):
        session = _mock_session({"free": "Swap:  2047  0  2047"})
        result = hc.check_memory(session, "web1")

        assert result.status == hc.STATUS_FAIL
        assert "not found" in result.message.lower()


# ──────────────────────────────────────────────────────────────────────────────
# Test 4 – HTTP /health check returns FAIL when the server is unreachable
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckHttpHealth:
    """check_http_health must return STATUS_FAIL on ConnectionError."""

    def test_connection_error_returns_fail(self):
        import requests as req

        cfg = _make_cfg()

        with patch("health_check.requests.get",
                   side_effect=req.exceptions.ConnectionError("refused")):
            result = hc.check_http_health(cfg)

        assert result.status == hc.STATUS_FAIL
        assert "connection refused" in result.message.lower()

    def test_timeout_returns_fail(self):
        import requests as req

        cfg = _make_cfg()

        with patch("health_check.requests.get",
                   side_effect=req.exceptions.Timeout()):
            result = hc.check_http_health(cfg)

        assert result.status == hc.STATUS_FAIL
        assert "timeout" in result.message.lower()

    def test_200_ok_body_returns_ok(self):
        cfg = _make_cfg()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok\n"

        with patch("health_check.requests.get", return_value=mock_resp):
            result = hc.check_http_health(cfg)

        assert result.status == hc.STATUS_OK
        assert result.value == "ok"

    def test_wrong_body_returns_fail(self):
        cfg = _make_cfg()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "maintenance"

        with patch("health_check.requests.get", return_value=mock_resp):
            result = hc.check_http_health(cfg)

        assert result.status == hc.STATUS_FAIL


# ──────────────────────────────────────────────────────────────────────────────
# Test 5 – Overall status is DEGRADED when exactly one check fails
# ──────────────────────────────────────────────────────────────────────────────

class TestOverallStatus:
    """run_checks must set overall=DEGRADED when SSH succeeds but one metric fails."""

    def _make_ok(self, name: str) -> hc.CheckResult:
        return hc.CheckResult(name=name, status=hc.STATUS_OK, value="ok")

    def _make_fail(self, name: str, msg: str = "err") -> hc.CheckResult:
        return hc.CheckResult(name=name, status=hc.STATUS_FAIL, message=msg)

    def test_all_ok_is_healthy(self):
        checks = [
            self._make_ok("SSH"),
            self._make_ok("CPU"),
            self._make_ok("MEM"),
            self._make_ok("DISK"),
            self._make_ok("NGINX"),
            self._make_ok("HTTP"),
            self._make_ok("/health"),
        ]
        report = hc.ServerReport(server="web1", checks=checks,
                                 overall=hc.OVERALL_HEALTHY, elapsed_s=0.5)
        assert report.overall == hc.OVERALL_HEALTHY

    def test_one_metric_fail_is_degraded(self):
        """Simulate a single NGINX service failure; SSH succeeds."""
        cfg = _make_cfg()

        with (
            patch("health_check.check_ssh",
                  return_value=(hc.CheckResult("SSH", hc.STATUS_OK), MagicMock(spec=hc.SSHSession))),
            patch("health_check.check_cpu",
                  return_value=hc.CheckResult("CPU", hc.STATUS_OK, "5.0%")),
            patch("health_check.check_memory",
                  return_value=hc.CheckResult("MEM", hc.STATUS_OK, "30.0%")),
            patch("health_check.check_disk",
                  return_value=hc.CheckResult("DISK", hc.STATUS_OK, "18%")),
            patch("health_check.check_nginx_service",
                  return_value=hc.CheckResult("NGINX", hc.STATUS_FAIL, message="inactive")),
            patch("health_check.check_http_home",
                  return_value=hc.CheckResult("HTTP", hc.STATUS_OK, "200")),
            patch("health_check.check_http_health",
                  return_value=hc.CheckResult("/health", hc.STATUS_OK, "ok")),
        ):
            report = hc.run_checks(cfg)

        assert report.overall == hc.OVERALL_DEGRADED, (
            "Expected DEGRADED when NGINX fails, got %s" % report.overall
        )

    def test_ssh_fail_is_unreachable(self):
        """When SSH itself fails the server should be UNREACHABLE."""
        cfg = _make_cfg()

        with (
            patch("health_check.check_ssh",
                  return_value=(hc.CheckResult("SSH", hc.STATUS_FAIL, message="refused"), None)),
            patch("health_check.check_http_home",
                  return_value=hc.CheckResult("HTTP", hc.STATUS_FAIL, message="refused")),
            patch("health_check.check_http_health",
                  return_value=hc.CheckResult("/health", hc.STATUS_FAIL, message="refused")),
        ):
            report = hc.run_checks(cfg)

        assert report.overall == hc.OVERALL_UNREACHABLE
