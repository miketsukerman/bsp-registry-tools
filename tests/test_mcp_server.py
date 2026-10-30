"""
Tests for the BSP MCP server (bsp.mcp): triage, pins, jobs, validation and an
optional end-to-end stdio run.

The whole module is skipped when the optional ``mcp`` SDK is not installed
(``pip install bsp-registry-tools[mcp]``, Python 3.10+).
"""

import json
import os
import stat
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from bsp.mcp import notify as notify_mod
from bsp.mcp import triage
from bsp.mcp.jobs import Job, JobError, JobManager
from bsp.mcp.pins import bump_pin, list_pins
from bsp.mcp.server import selection_args, validate_free_value

# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

BITBAKE_TWO_FAILURES = textwrap.dedent("""\
    NOTE: Executing Tasks
    ERROR: busybox-1.36.1-r0 do_compile: oe_runmake failed
    ERROR: busybox-1.36.1-r0 do_compile: ExecutionError('/work/temp/run.do_compile.123', 1, None, None)
    ERROR: Logfile of failure stored in: /work/tmp/work/busybox/temp/log.do_compile.123
    coreutils.c:10:5: error: unknown type name 'foo'
    ERROR: Task (/layers/meta/recipes-core/busybox/busybox_1.36.1.bb:do_compile) failed with exit code '1'
    ERROR: linux-imx-6.1.55-r0 do_fetch: Fetcher failure: Unable to fetch URL from any source.
    ERROR: Logfile of failure stored in: /work/tmp/work/linux-imx/temp/log.do_fetch.456
    ERROR: Task (/layers/meta-freescale/recipes-kernel/linux/linux-imx_6.1.55.bb:do_fetch) failed with exit code '1'
    NOTE: Tasks Summary: Attempted 4000 tasks of which 3998 didn't need to be rerun and all succeeded.
    Summary: 2 tasks failed:
      /layers/meta/recipes-core/busybox/busybox_1.36.1.bb:do_compile
      /layers/meta-freescale/recipes-kernel/linux/linux-imx_6.1.55.bb:do_fetch
""")


class TestTriage:
    def test_two_failures_classified_independently(self):
        result = triage.analyze_log(BITBAKE_TWO_FAILURES)
        assert result["failed_task_count"] == 2
        failures = result["failures"]
        by_recipe = {f["recipe"]: f for f in failures}
        assert by_recipe["busybox"]["category"] == "compile"
        assert by_recipe["busybox"]["task"] == "do_compile"
        assert by_recipe["busybox"]["task_log"] == (
            "/work/tmp/work/busybox/temp/log.do_compile.123"
        )
        assert by_recipe["linux-imx"]["category"] == "fetch"
        assert by_recipe["linux-imx"]["task_log"] == (
            "/work/tmp/work/linux-imx/temp/log.do_fetch.456"
        )
        assert set(result["categories"]) == {"compile", "fetch"}

    def test_kas_user_error_is_kas_config(self):
        log = "ERROR: KasUserError: Error in configuration file: missing key\n"
        result = triage.analyze_log(log)
        assert result["failures"][0]["category"] == "kas_config"

    def test_disk_space_overrides_task(self):
        log = textwrap.dedent("""\
            ERROR: glibc-2.38-r0 do_compile: No space left on device
            ERROR: Task (/layers/meta/recipes-core/glibc/glibc_2.38.bb:do_compile) failed with exit code '1'
        """)
        result = triage.analyze_log(log)
        assert result["failures"][0]["category"] == "disk_space"

    def test_gnutls_recipe_compile_failure_is_not_network(self):
        log = textwrap.dedent("""\
            ERROR: gnutls-3.8.0-r0 do_compile: oe_runmake failed
            gnutls_cipher.c:55: error: implicit declaration of function
            ERROR: Task (/layers/meta/recipes-support/gnutls/gnutls_3.8.0.bb:do_compile) failed with exit code '1'
        """)
        result = triage.analyze_log(log)
        assert result["failures"][0]["category"] == "compile"

    def test_kas_clone_failure_fatal_line_network(self):
        log = textwrap.dedent("""\
            2024-01-01 10:00:00 - ERROR - Could not clone repository
            fatal: unable to access 'https://example.com/repo.git/': Could not resolve host: example.com
            2024-01-01 10:00:00 - ERROR - fetch repos failed
        """)
        result = triage.analyze_log(log)
        failure = result["failures"][0]
        assert failure["category"] == "network"
        assert any("Could not resolve host" in ln for ln in failure["evidence"])
        assert any(ln.startswith("fatal:") for ln in failure["evidence"])

    def test_read_task_log_refuses_outside_roots(self, tmp_path):
        with pytest.raises(PermissionError):
            triage.read_task_log("/etc/passwd", [tmp_path])

    def test_read_task_log_inside_root(self, tmp_path):
        log = tmp_path / "log.do_compile"
        log.write_text("line1\nline2\n")
        assert triage.read_task_log(str(log), [tmp_path]) == "line1\nline2"


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

BASE_YAML = textwrap.dedent("""\
    header:
      version: 14
    repos:
      meta-foo:
        url: https://example.com/meta-foo.git
        branch: kirkstone
      meta-bar:
        url: https://example.com/meta-bar.git
        branch: kirkstone
""")

PINNED_YAML = textwrap.dedent("""\
    header:
      version: 14
      includes:
        - base.yml
    repos:
      meta-foo:
        # pinned for release 1.2
        commit: 1111111111111111111111111111111111111111
      meta-bar:
        commit: "2222222222222222222222222222222222222222"  # keep in sync
""")


@pytest.fixture
def pin_tree(tmp_path):
    (tmp_path / "base.yml").write_text(BASE_YAML)
    (tmp_path / "pinned.yml").write_text(PINNED_YAML)
    return tmp_path


class TestPins:
    def test_inherited_url_is_resolved(self, pin_tree):
        pins = {p["repo"]: p for p in list_pins(pin_tree)}
        assert pins["meta-foo"]["url"] == "https://example.com/meta-foo.git"
        assert pins["meta-foo"]["branch"] == "kirkstone"
        assert pins["meta-foo"]["commit"] == "1" * 40
        assert pins["meta-bar"]["commit"] == "2" * 40

    def test_bump_preserves_comments_and_other_repos(self, pin_tree):
        new_sha = "a" * 40
        result = bump_pin(pin_tree, "pinned.yml", "meta-bar", new_sha)
        assert result == {"old_commit": "2" * 40, "new_commit": new_sha}
        text = (pin_tree / "pinned.yml").read_text()
        assert "# pinned for release 1.2" in text
        assert f'commit: "{new_sha}"  # keep in sync' in text
        assert "commit: " + "1" * 40 in text  # other repo untouched
        assert (pin_tree / "base.yml").read_text() == BASE_YAML

    def test_bump_rejects_bad_sha(self, pin_tree):
        with pytest.raises(ValueError):
            bump_pin(pin_tree, "pinned.yml", "meta-foo", "deadbeef")
        with pytest.raises(ValueError):
            bump_pin(pin_tree, "pinned.yml", "meta-foo", "A" * 40)

    def test_bump_rejects_missing_repo(self, pin_tree):
        with pytest.raises(LookupError):
            bump_pin(pin_tree, "pinned.yml", "meta-nope", "a" * 40)

    def test_bump_rejects_path_escape(self, pin_tree):
        with pytest.raises(ValueError):
            bump_pin(pin_tree, "../pinned.yml", "meta-foo", "a" * 40)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_bsp(tmp_path):
    """A fake 'bsp' script controlled by its first argument."""
    script = tmp_path / "fake-bsp"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        case "$1" in
          ok) echo "build ok"; exit 0 ;;
          fail) echo "build failed" >&2; exit 3 ;;
          sleep) echo "sleeping"; sleep 60 ;;
          nap) echo "napping"; sleep 1; echo "done"; exit 0 ;;
          echodir) echo "DIR=$2"; exit 0 ;;
        esac
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _wait_terminal(manager, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job.state in ("succeeded", "failed", "cancelled", "lost"):
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish: {manager.get(job_id).state}")


class TestJobs:
    def test_success(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        job = _wait_terminal(mgr, job.id)
        assert job.state == "succeeded"
        assert job.return_code == 0
        log = mgr.read_log(job.id)
        assert "build ok" in log
        assert log.splitlines()[0].startswith("$ ")  # command on first line
        assert job.summary()["duration"] is not None

    def test_failure(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "fail"])
        job = _wait_terminal(mgr, job.id)
        assert job.state == "failed"
        assert job.return_code == 3
        assert "build failed" in mgr.read_log(job.id)

    def test_persistence_across_managers(self, tmp_path, fake_bsp):
        state = tmp_path / "state"
        mgr = JobManager(state_dir=state)
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(mgr, job.id)

        mgr2 = JobManager(state_dir=state)
        reloaded = mgr2.get(job.id)
        assert reloaded.state == "succeeded"

    def test_non_terminal_jobs_become_lost(self, tmp_path):
        state = tmp_path / "state"
        job_dir = state / "20240101-000000-deadbeef"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({
            "id": "20240101-000000-deadbeef",
            "kind": "build",
            "target": "demo",
            "command": ["bsp", "build"],
            "state": "running",
            "job_dir": str(job_dir),
        }))
        mgr = JobManager(state_dir=state)
        assert mgr.get("20240101-000000-deadbeef").state == "lost"

    def test_concurrency_limit(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state", max_jobs=1)
        job = mgr.start("build", "demo", [fake_bsp, "sleep"])
        try:
            with pytest.raises(JobError) as exc:
                mgr.start("build", "demo2", [fake_bsp, "ok"])
            assert job.id in str(exc.value)
        finally:
            # Wait until pid is known, then cancel.
            deadline = time.time() + 10
            while mgr.get(job.id).pid is None and time.time() < deadline:
                time.sleep(0.05)
            mgr.cancel(job.id)
            _wait_terminal(mgr, job.id)

    def test_cancel_stays_cancelled(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "sleep"])
        deadline = time.time() + 10
        while mgr.get(job.id).pid is None and time.time() < deadline:
            time.sleep(0.05)
        mgr.cancel(job.id)
        job = _wait_terminal(mgr, job.id)
        assert job.state == "cancelled"

    def test_job_dir_substitution(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("scan", "demo", [fake_bsp, "echodir", "{job_dir}/reports"],
                        extra={"reports_dir": "{job_dir}/reports"})
        assert "{job_dir}" not in " ".join(job.command)
        assert job.extra["reports_dir"] == f"{job.job_dir}/reports"
        job = _wait_terminal(mgr, job.id)
        assert f"DIR={job.job_dir}/reports" in mgr.read_log(job.id)

    def test_read_log_bad_regex(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(mgr, job.id)
        with pytest.raises(JobError, match="Invalid grep regex"):
            mgr.read_log(job.id, grep="[unclosed")


# ---------------------------------------------------------------------------
# selection_args validation
# ---------------------------------------------------------------------------


class TestSelectionArgs:
    def test_rejects_flag_injection(self):
        with pytest.raises(ValueError, match="Invalid bsp_name"):
            selection_args(bsp_name="--clean")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            selection_args(device="rsb 4810", release="scarthgap")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            selection_args(bsp_name="")

    def test_rejects_both_selections(self):
        with pytest.raises(ValueError, match="not both"):
            selection_args(bsp_name="demo", device="rsb-4810", release="scarthgap")

    def test_valid_selection(self):
        assert selection_args(bsp_name="demo-bsp") == ["demo-bsp"]
        assert selection_args(device="rsb-4810", release="scarthgap",
                              features=["docker"]) == [
            "--device", "rsb-4810", "--release", "scarthgap", "--feature", "docker",
        ]


class TestFreeValue:
    def test_accepts_paths_urls_and_templates(self):
        assert validate_free_value("/dev/sdb", "target") == "/dev/sdb"
        assert validate_free_value("{device}/{release}", "prefix")
        assert validate_free_value("https://example.com/repo.git", "url")

    def test_rejects_flag_injection(self):
        with pytest.raises(ValueError, match="Invalid target"):
            validate_free_value("--force", "target")

    def test_rejects_empty_and_control_chars(self):
        with pytest.raises(ValueError):
            validate_free_value("", "prefix")
        with pytest.raises(ValueError):
            validate_free_value("a\nb", "prefix")


class TestStartBuildTool:
    """Command construction for the start_build tool (no subprocess run)."""

    @pytest.fixture
    def server_and_calls(self, tmp_path):
        from bsp.mcp.server import ServerContext, build_server

        registry = tmp_path / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        calls = []

        class FakeJob:
            def summary(self):
                return {"id": "job-1", "state": "running"}

        def fake_start(kind, name, cmd, extra=None):
            calls.append((kind, name, cmd))
            return FakeJob()

        ctx.jobs.start = fake_start
        return build_server(ctx), calls

    async def test_target_is_forwarded(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_build", {
            "bsp_name": "demo-bsp", "target": "core-image-minimal",
        })
        kind, name, cmd = calls[0]
        assert kind == "build" and name == "demo-bsp"
        idx = cmd.index("--target")
        assert cmd[idx + 1] == "core-image-minimal"

    async def test_no_target_by_default(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_build", {"bsp_name": "demo-bsp"})
        assert "--target" not in calls[0][2]

    async def test_target_injection_rejected(self, server_and_calls):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="Invalid target"):
            await server.call_tool("start_build", {
                "bsp_name": "demo-bsp", "target": "--clean",
            })
        assert not calls


class TestStartHilTestSshOverrides:
    """ssh_host/ssh_port forwarding and validation in start_hil_test."""

    @pytest.fixture
    def server_and_calls(self, tmp_path):
        from bsp.mcp.server import ServerContext, build_server

        registry = tmp_path / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        calls = []

        class FakeJob:
            def summary(self):
                return {"id": "job-1", "state": "running"}

        def fake_start(kind, name, cmd, extra=None):
            calls.append((kind, name, cmd, extra))
            return FakeJob()

        ctx.jobs.start = fake_start
        return build_server(ctx), calls

    async def test_host_and_port_forwarded(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-ssh",
            "ssh_host": "192.168.3.42", "ssh_port": 2222,
        })
        _, _, cmd, extra = calls[0]
        joined = " ".join(cmd)
        assert "--ssh-host 192.168.3.42 --ssh-port 2222" in joined
        assert extra == {"reports_dir": "{job_dir}/test-results",
                         "ssh_host": "192.168.3.42", "ssh_port": "2222"}

    async def test_command_unchanged_without_overrides(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-ssh",
        })
        _, _, cmd, extra = calls[0]
        assert "--ssh-host" not in cmd and "--ssh-port" not in cmd
        assert extra == {"reports_dir": "{job_dir}/test-results"}
        idx = cmd.index("--direct-output-dir")
        assert cmd[idx + 1] == "{job_dir}/test-results"

    @pytest.mark.parametrize("backend",
                             ["direct-local", "direct-ssh", "direct-serial"])
    async def test_direct_backends_get_per_job_output_dir(
            self, server_and_calls, backend):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": backend,
        })
        _, _, cmd, extra = calls[0]
        assert cmd[cmd.index("--direct-output-dir") + 1] == "{job_dir}/test-results"
        assert extra["reports_dir"] == "{job_dir}/test-results"

    async def test_lava_backend_command_unchanged(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "lava",
        })
        _, _, cmd, extra = calls[0]
        assert "--direct-output-dir" not in cmd
        assert extra is None
        assert cmd[-3:] == ["--backend", "lava", "--wait"]

    @pytest.mark.parametrize("host", ["-oProxyCommand=x", "a b", "root@h", ""])
    async def test_rejected_hosts(self, server_and_calls, host):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="ssh_host"):
            await server.call_tool("start_hil_test", {
                "bsp_name": "demo-bsp", "backend": "direct-ssh",
                "ssh_host": host,
            })
        assert not calls

    @pytest.mark.parametrize("port", [0, 70000])
    async def test_rejected_ports(self, server_and_calls, port):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="ssh_port"):
            await server.call_tool("start_hil_test", {
                "bsp_name": "demo-bsp", "backend": "direct-ssh",
                "ssh_port": port,
            })
        assert not calls

    async def test_rejected_for_lava_backend(self, server_and_calls):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError,
                           match="only apply to direct-ssh and direct-serial"):
            await server.call_tool("start_hil_test", {
                "bsp_name": "demo-bsp", "backend": "lava",
                "ssh_host": "192.168.3.42",
            })
        assert not calls

    @pytest.mark.parametrize("host", ["fd00::2820:ed93", "rom2820-ed93.local"])
    async def test_accepted_hosts(self, server_and_calls, host):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-serial",
            "ssh_host": host,
        })
        _, _, cmd, extra = calls[0]
        assert cmd[cmd.index("--ssh-host") + 1] == host
        assert extra["ssh_host"] == host

    async def test_test_suites_forwarded(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-ssh",
            "test_suites": ["adv-context", "adv-cpu"],
        })
        _, _, cmd, _extra = calls[0]
        joined = " ".join(cmd)
        assert "--test-suite adv-context" in joined
        assert "--test-suite adv-cpu" in joined

    async def test_no_test_suite_flag_by_default(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-ssh",
        })
        assert "--test-suite" not in calls[0][2]

    @pytest.mark.parametrize("suite", ["--flag", "a b", "", "x" * 65, "a/b"])
    async def test_invalid_test_suite_rejected(self, server_and_calls, suite):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="test suite"):
            await server.call_tool("start_hil_test", {
                "bsp_name": "demo-bsp", "backend": "direct-ssh",
                "test_suites": [suite],
            })
        assert not calls

    async def test_job_status_reports_ssh_host(self, tmp_path, fake_bsp):
        from bsp.mcp.server import ServerContext, build_server

        registry = tmp_path / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        ctx.cli_command = lambda: [fake_bsp, "ok"]
        server = build_server(ctx)
        await server.call_tool("start_hil_test", {
            "bsp_name": "demo-bsp", "backend": "direct-ssh",
            "ssh_host": "192.168.3.42", "ssh_port": 2222,
        })
        job = ctx.jobs.list()[0]
        _wait_terminal(ctx.jobs, job.id)
        status = ctx.jobs.get(job.id).summary()
        assert status["extra"]["ssh_host"] == "192.168.3.42"
        assert status["extra"]["ssh_port"] == "2222"


class TestTestReport:
    """test_report / test_report_file on a fixture direct-test-summary.json."""

    SUMMARY = {
        "label": "demo-bsp",
        "backend": "direct-ssh",
        "passed": False,
        "suite_count": 2,
        "suites": [
            {
                "name": "context",
                "status": "pass",
                "duration": 3.1,
                "cases": [
                    {
                        "name": "kernel-version",
                        "status": "pass",
                        "duration": 1.0,
                        "timed_out": False,
                        "log_path": None,
                        "lava_signals": [
                            {"test_case_id": "kernel-version",
                             "result": "pass",
                             "requirement_id": "BSP-CTX-002",
                             "measurement": None, "units": None},
                        ],
                    },
                    {
                        # No signals: falls back to the case status.
                        "name": "hostname",
                        "status": "pass",
                        "duration": 1.0,
                        "timed_out": False,
                        "log_path": None,
                        "lava_signals": [],
                    },
                    {
                        "name": "optional-feature",
                        "status": "pass",
                        "duration": 1.1,
                        "timed_out": False,
                        "log_path": None,
                        "lava_signals": [
                            {"test_case_id": "optional-feature",
                             "result": "skip"},
                        ],
                    },
                ],
            },
            {
                "name": "cpu",
                "status": "fail",
                "duration": 38.1,
                "cases": [
                    {
                        "name": "cpu-model",
                        "status": "fail",
                        "duration": 20.0,
                        "timed_out": False,
                        "log_path": "LOG_INSIDE",
                        "lava_signals": [
                            {"test_case_id": "cpu-model", "result": "fail"},
                        ],
                    },
                    {
                        "name": "cpu-outside",
                        "status": "fail",
                        "duration": 10.0,
                        "timed_out": False,
                        "log_path": "LOG_OUTSIDE",
                        "lava_signals": [
                            {"test_case_id": "cpu-outside", "result": "fail"},
                        ],
                    },
                    {
                        "name": "cpu-freq",
                        "status": "pass",
                        "duration": 8.1,
                        "timed_out": False,
                        "log_path": None,
                        "lava_signals": [
                            {"test_case_id": "cpu-freq",
                             "result": "inconclusive"},
                        ],
                    },
                ],
            },
        ],
    }

    @pytest.fixture
    def report_env(self, tmp_path):
        from bsp.mcp.jobs import Job
        from bsp.mcp.server import ServerContext, build_server

        reg_dir = tmp_path / "reg"
        reg_dir.mkdir()
        registry = reg_dir / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        server = build_server(ctx)

        job_dir = ctx.state_dir / "job-test-1"
        reports = job_dir / "test-results"
        reports.mkdir(parents=True)
        inside_log = reports / "cpu-model.log"
        inside_log.write_text(
            "\n".join(f"line{i}" for i in range(1, 31)) + "\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_log = outside / "cpu-outside.log"
        outside_log.write_text("device secret\n")

        summary = json.loads(
            json.dumps(self.SUMMARY)
            .replace("LOG_INSIDE", str(inside_log))
            .replace("LOG_OUTSIDE", str(outside_log))
        )
        (reports / "direct-test-summary.json").write_text(json.dumps(summary))
        (reports / "direct-test-report.html").write_text("<html>report</html>")

        job = Job(id="job-test-1", kind="test", target="demo-bsp",
                  command=["bsp", "test"], state="failed",
                  job_dir=str(job_dir),
                  extra={"reports_dir": str(reports),
                         "ssh_host": "192.168.3.242"})
        ctx.jobs._jobs[job.id] = job
        return server, ctx, reports

    @staticmethod
    async def _call(server, name, args):
        result = await server.call_tool(name, args)
        content = getattr(result, "content", result)
        return json.loads(content[0].text)

    async def test_totals_suites_and_report_files(self, report_env):
        server, _, reports = report_env
        result = await self._call(server, "test_report",
                                  {"job_id": "job-test-1"})
        assert result["job_id"] == "job-test-1"
        assert result["job_state"] == "failed"
        assert result["passed"] is False
        assert result["label"] == "demo-bsp"
        assert result["backend"] == "direct-ssh"
        assert result["ssh_host"] == "192.168.3.242"
        assert result["totals"] == {
            "suites": 2, "cases": 6, "pass": 2, "fail": 2,
            "skip": 1, "unknown": 1, "duration_s": 41.2,
        }
        assert result["truncated"] is False
        assert result["report_files"]["json"] == str(
            reports / "direct-test-summary.json")
        assert result["report_files"]["html"] == str(
            reports / "direct-test-report.html")
        assert result["report_files"]["pdf"] is None

        context = next(s for s in result["suites"] if s["name"] == "context")
        by_name = {c["name"]: c for c in context["cases"]}
        assert by_name["hostname"]["result"] == "pass"  # no signals: status
        assert by_name["kernel-version"]["requirement_id"] == "BSP-CTX-002"
        assert by_name["optional-feature"]["result"] == "skip"

    async def test_failures_and_log_excerpts(self, report_env):
        server, _, _ = report_env
        result = await self._call(server, "test_report",
                                  {"job_id": "job-test-1"})
        failures = {f["case"]: f for f in result["failures"]}
        assert set(failures) == {"cpu-model", "cpu-outside"}
        excerpt = failures["cpu-model"]["log_excerpt"]
        assert excerpt.splitlines() == [f"line{i}" for i in range(11, 31)]
        # Log outside the allowed roots is never read.
        assert failures["cpu-outside"]["log_excerpt"] is None
        assert failures["cpu-model"]["suite"] == "cpu"

    async def test_max_cases_truncates_but_totals_complete(self, report_env):
        server, _, _ = report_env
        result = await self._call(server, "test_report", {
            "job_id": "job-test-1", "max_cases": 2,
        })
        assert result["truncated"] is True
        returned = sum(len(s["cases"]) for s in result["suites"])
        assert returned == 2
        assert result["totals"]["cases"] == 6

    async def test_running_job_rejected(self, report_env):
        from bsp.mcp.jobs import Job
        from bsp.mcp.server import ToolError
        server, ctx, _ = report_env
        ctx.jobs._jobs["job-run"] = Job(
            id="job-run", kind="test", target="demo", command=[],
            state="running", job_dir=str(ctx.state_dir / "job-run"))
        with pytest.raises(ToolError, match="still running"):
            await server.call_tool("test_report", {"job_id": "job-run"})

    async def test_missing_summary_names_path_and_hint(self, report_env):
        from bsp.mcp.jobs import Job
        from bsp.mcp.server import ToolError
        server, ctx, _ = report_env
        job_dir = ctx.state_dir / "job-empty"
        job_dir.mkdir()
        ctx.jobs._jobs["job-empty"] = Job(
            id="job-empty", kind="test", target="demo", command=[],
            state="failed", job_dir=str(job_dir))
        with pytest.raises(ToolError) as exc:
            await server.call_tool("test_report", {"job_id": "job-empty"})
        assert "direct-test-summary.json" in str(exc.value)
        assert "the run failed before tests started; see job_log" in str(exc.value)

    async def test_non_test_job_rejected(self, report_env):
        from bsp.mcp.jobs import Job
        from bsp.mcp.server import ToolError
        server, ctx, _ = report_env
        ctx.jobs._jobs["job-build"] = Job(
            id="job-build", kind="build", target="demo", command=[],
            state="succeeded", job_dir=str(ctx.state_dir / "job-build"))
        with pytest.raises(ToolError, match="not a test job"):
            await server.call_tool("test_report", {"job_id": "job-build"})

    async def test_report_file_returns_html(self, report_env):
        server, _, _ = report_env
        result = await server.call_tool("test_report_file", {
            "job_id": "job-test-1", "kind": "html",
        })
        content = getattr(result, "content", result)
        assert content[0].text == "<html>report</html>"

    async def test_report_file_rejects_unknown_kind(self, report_env):
        from bsp.mcp.server import ToolError
        server, _, _ = report_env
        with pytest.raises(ToolError, match="kind must be one of"):
            await server.call_tool("test_report_file", {
                "job_id": "job-test-1", "kind": "pdf",
            })

    async def test_report_file_rejects_oversized(self, report_env):
        from bsp.mcp.server import ToolError
        server, _, reports = report_env
        (reports / "direct-test-report.html").write_text(
            "x" * (2 * 1024 * 1024 + 1))
        with pytest.raises(ToolError, match="2 MB"):
            await server.call_tool("test_report_file", {
                "job_id": "job-test-1", "kind": "html",
            })


class TestNewJobKinds:
    @pytest.mark.parametrize(
        "kind", ["fetch", "deploy", "flash", "gather", "container_build"]
    )
    def test_new_kinds_accepted(self, tmp_path, fake_bsp, kind):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start(kind, "demo", [fake_bsp, "ok"])
        job = _wait_terminal(mgr, job.id)
        assert job.kind == kind
        assert job.state == "succeeded"


# ---------------------------------------------------------------------------
# wait_job (JobManager.wait + the tool)
# ---------------------------------------------------------------------------


def _wait_pid(manager, job_id, timeout=10):
    deadline = time.time() + timeout
    while manager.get(job_id).pid is None and time.time() < deadline:
        time.sleep(0.05)


class TestJobManagerWait:
    def test_returns_immediately_for_finished_job(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(mgr, job.id)
        # The job ended before the wait started: no lost wake-up.
        start = time.monotonic()
        assert mgr.wait(job.id, timeout=10) is True
        assert time.monotonic() - start < 1

    def test_wakes_when_job_finishes(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "nap"])
        start = time.monotonic()
        assert mgr.wait(job.id, timeout=30) is True
        assert time.monotonic() - start < 15  # far less than the timeout
        assert mgr.get(job.id).state == "succeeded"

    def test_times_out_for_running_job(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "sleep"])
        try:
            assert mgr.wait(job.id, timeout=0.3) is False
        finally:
            _wait_pid(mgr, job.id)
            mgr.cancel(job.id)
            _wait_terminal(mgr, job.id)

    def test_wakes_on_cancel(self, tmp_path, fake_bsp):
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "sleep"])
        _wait_pid(mgr, job.id)

        def _cancel():
            time.sleep(0.2)
            mgr.cancel(job.id)

        threading.Thread(target=_cancel, daemon=True).start()
        start = time.monotonic()
        assert mgr.wait(job.id, timeout=30) is True
        assert time.monotonic() - start < 15
        assert mgr.get(job.id).state == "cancelled"
        _wait_terminal(mgr, job.id)

    def test_unknown_job_rejected(self, tmp_path):
        mgr = JobManager(state_dir=tmp_path / "state")
        with pytest.raises(JobError, match="Unknown job id"):
            mgr.wait("nope", timeout=0.1)


class TestWaitJobTool:
    @pytest.fixture
    def env(self, tmp_path, fake_bsp):
        from bsp.mcp.server import ServerContext, build_server

        registry = tmp_path / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        return build_server(ctx), ctx

    @staticmethod
    async def _call(server, args):
        result = await server.call_tool("wait_job", args)
        content = getattr(result, "content", result)
        return json.loads(content[0].text)

    async def test_finished_job_returns_at_once(self, env, fake_bsp):
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(ctx.jobs, job.id)
        result = await self._call(server, {"job_id": job.id, "timeout_s": 60})
        assert result["terminal"] is True
        assert result["state"] == "succeeded"
        assert result["waited_s"] < 5
        assert result["hint"] == "job finished"
        assert "build ok" in result["log_tail"]

    async def test_returns_when_running_job_finishes(self, env, fake_bsp):
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "nap"])
        result = await self._call(server, {"job_id": job.id, "timeout_s": 60})
        assert result["terminal"] is True
        assert result["state"] == "succeeded"
        assert result["waited_s"] < 30  # did not wait for the whole timeout

    async def test_timeout_returns_terminal_false(self, env, fake_bsp):
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "sleep"])
        try:
            result = await self._call(server, {"job_id": job.id, "timeout_s": 1})
            assert result["terminal"] is False
            assert result["state"] == "running"
            assert result["hint"].startswith("still running")
            assert result["waited_s"] >= 1
        finally:
            _wait_pid(ctx.jobs, job.id)
            ctx.jobs.cancel(job.id)
            _wait_terminal(ctx.jobs, job.id)

    async def test_timeout_clamped_to_wait_max(self, env, fake_bsp, monkeypatch):
        monkeypatch.setenv("BSP_MCP_WAIT_MAX_S", "1")
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "sleep"])
        try:
            start = time.monotonic()
            result = await self._call(server, {"job_id": job.id,
                                               "timeout_s": 99999})
            assert result["terminal"] is False
            assert time.monotonic() - start < 10
        finally:
            _wait_pid(ctx.jobs, job.id)
            ctx.jobs.cancel(job.id)
            _wait_terminal(ctx.jobs, job.id)

    async def test_timeout_clamped_to_at_least_one_second(self, env, fake_bsp):
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "ok"])
        result = await self._call(server, {"job_id": job.id, "timeout_s": 0})
        assert "terminal" in result  # clamped to 1, no crash

    async def test_wakes_on_cancel(self, env, fake_bsp):
        server, ctx = env
        job = ctx.jobs.start("build", "demo", [fake_bsp, "sleep"])
        _wait_pid(ctx.jobs, job.id)

        def _cancel():
            time.sleep(0.2)
            ctx.jobs.cancel(job.id)

        threading.Thread(target=_cancel, daemon=True).start()
        result = await self._call(server, {"job_id": job.id, "timeout_s": 60})
        assert result["terminal"] is True
        assert result["state"] == "cancelled"
        assert result["waited_s"] < 30
        _wait_terminal(ctx.jobs, job.id)

    async def test_unknown_job_rejected(self, env):
        from bsp.mcp.server import ToolError
        server, _ = env
        with pytest.raises(ToolError, match="Unknown job id"):
            await server.call_tool("wait_job", {"job_id": "nope"})


# ---------------------------------------------------------------------------
# Completion notifications (webhook)
# ---------------------------------------------------------------------------


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.recorded.append(
            (self.path, dict(self.headers), json.loads(body)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook_server():
    server = HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    server.recorded = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _wait_notify(manager, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        outcome = manager.get(job_id).extra.get("notify")
        if outcome is not None:
            return outcome
        time.sleep(0.05)
    pytest.fail(f"job {job_id} never recorded a notify outcome")


def _make_job(tmp_path, **overrides):
    job_dir = tmp_path / "job-notify"
    job_dir.mkdir(exist_ok=True)
    fields = {"id": "20240101-000000-cafecafe", "kind": "build",
              "target": "demo", "command": ["bsp", "build"],
              "state": "succeeded", "return_code": 0,
              "started_at": 1000.0, "finished_at": 1090.0,
              "job_dir": str(job_dir), "extra": {}}
    fields.update(overrides)
    return Job(**fields)


class TestNotify:
    def test_disabled_without_url(self, tmp_path, fake_bsp, monkeypatch,
                                  webhook_server):
        monkeypatch.delenv("BSP_MCP_NOTIFY_URL", raising=False)
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(mgr, job.id)
        assert _wait_notify(mgr, job.id) == "disabled"
        assert webhook_server.recorded == []
        # The outcome is persisted.
        on_disk = json.loads((Path(job.job_dir) / "job.json").read_text())
        assert on_disk["extra"]["notify"] == "disabled"

    def test_openclaw_format_and_token(self, tmp_path, monkeypatch,
                                       webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hooks/agent"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        monkeypatch.setenv("BSP_MCP_NOTIFY_TOKEN_ENV", "MY_HOOK_TOKEN")
        monkeypatch.setenv("MY_HOOK_TOKEN", "sekrit-token")
        job = _make_job(tmp_path, extra={"notify_agent": "builder",
                                         "notify_session": "agent:main:s1"})
        assert notify_mod.notify_job(job) == "sent"
        path, headers, payload = webhook_server.recorded[0]
        assert path == "/hooks/agent"
        assert headers["Authorization"] == "Bearer sekrit-token"
        assert headers["Idempotency-Key"] == (
            f"bsp-mcp-{job.id}-succeeded")
        assert payload["name"] == f"bsp-mcp job {job.id} succeeded"
        assert payload["agentId"] == "builder"
        assert payload["sessionKey"] == "agent:main:s1"
        assert payload["deliver"] is False
        assert f"({job.kind} {job.target}) finished: succeeded" in payload["message"]
        assert "Exit code 0" in payload["message"]
        # The token never leaks into the payload or the job summary.
        assert "sekrit-token" not in json.dumps(payload)
        assert "sekrit-token" not in json.dumps(job.summary())

    def test_openclaw_agent_defaults(self, tmp_path, monkeypatch,
                                     webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hooks/agent"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        monkeypatch.delenv("BSP_MCP_NOTIFY_AGENT", raising=False)
        assert notify_mod.notify_job(_make_job(tmp_path)) == "sent"
        payload = webhook_server.recorded[0][2]
        assert payload["agentId"] == "main"
        assert "sessionKey" not in payload
        monkeypatch.setenv("BSP_MCP_NOTIFY_AGENT", "tester")
        assert notify_mod.notify_job(_make_job(tmp_path)) == "sent"
        assert webhook_server.recorded[1][2]["agentId"] == "tester"

    def test_generic_format(self, tmp_path, monkeypatch, webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hook"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        monkeypatch.setenv("BSP_MCP_NOTIFY_FORMAT", "generic")
        job = _make_job(tmp_path)
        assert notify_mod.notify_job(job) == "sent"
        payload = webhook_server.recorded[0][2]
        assert payload["event"] == "job.finished"
        assert payload["job"]["id"] == job.id
        assert payload["job"]["state"] == "succeeded"
        assert payload["server"]

    def test_retries_then_failed_and_state_unchanged(self, tmp_path,
                                                     monkeypatch):
        import socket

        # Reserve a port and close it: connections are refused.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", f"http://127.0.0.1:{port}/x")
        monkeypatch.setattr(notify_mod, "RETRY_DELAYS_S", (0, 0, 0))
        attempts = []
        real_urlopen = notify_mod.urllib.request.urlopen

        def counting_urlopen(*args, **kwargs):
            attempts.append(1)
            return real_urlopen(*args, **kwargs)

        monkeypatch.setattr(notify_mod.urllib.request, "urlopen",
                            counting_urlopen)
        job = _make_job(tmp_path)
        outcome = notify_mod.notify_job(job)
        assert outcome.startswith("failed: ")
        assert len(attempts) == 4  # first try + 3 retries
        assert job.state == "succeeded"  # never changed by the notification

    def test_kinds_filter(self, tmp_path, monkeypatch, webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hook"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        monkeypatch.setenv("BSP_MCP_NOTIFY_KINDS", "deploy, test")
        assert notify_mod.notify_job(_make_job(tmp_path, kind="build")) == "disabled"
        assert webhook_server.recorded == []
        assert notify_mod.notify_job(_make_job(tmp_path, kind="deploy")) == "sent"
        assert len(webhook_server.recorded) == 1

    def test_manager_sends_once_per_job(self, tmp_path, fake_bsp, monkeypatch,
                                        webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hooks/agent"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        mgr = JobManager(state_dir=tmp_path / "state")
        job = mgr.start("build", "demo", [fake_bsp, "ok"])
        _wait_terminal(mgr, job.id)
        assert _wait_notify(mgr, job.id) == "sent"
        assert len(webhook_server.recorded) == 1

    def test_lost_jobs_notified_at_startup(self, tmp_path, monkeypatch,
                                           webhook_server):
        url = f"http://127.0.0.1:{webhook_server.server_port}/hooks/agent"
        monkeypatch.setenv("BSP_MCP_NOTIFY_URL", url)
        state = tmp_path / "state"
        job_dir = state / "20240101-000000-deadbeef"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({
            "id": "20240101-000000-deadbeef",
            "kind": "build",
            "target": "demo",
            "command": ["bsp", "build"],
            "state": "running",
            "job_dir": str(job_dir),
        }))
        mgr = JobManager(state_dir=state)
        assert _wait_notify(mgr, "20240101-000000-deadbeef") == "sent"
        payload = webhook_server.recorded[0][2]
        assert payload["name"].endswith(" lost")


class TestNotifyRouting:
    """notify_agent/notify_session on the start_* tools."""

    @pytest.fixture
    def server_and_calls(self, tmp_path):
        from bsp.mcp.server import ServerContext, build_server

        registry = tmp_path / "bsp-registry.yml"
        registry.write_text("registry: {}\n")
        ctx = ServerContext(registry=str(registry),
                            state_dir=str(tmp_path / "state"))
        calls = []

        class FakeJob:
            def summary(self):
                return {"id": "job-1", "state": "running"}

        def fake_start(kind, name, cmd, extra=None):
            calls.append((kind, name, cmd, extra))
            return FakeJob()

        ctx.jobs.start = fake_start
        return build_server(ctx), calls

    @pytest.mark.parametrize("tool_name,args", [
        ("start_build", {"bsp_name": "demo-bsp"}),
        ("start_fetch", {"bsp_name": "demo-bsp"}),
        ("start_deploy", {"bsp_name": "demo-bsp"}),
        ("start_gather", {"bsp_name": "demo-bsp"}),
        ("start_flash", {"bsp_name": "demo-bsp"}),
        ("start_scan", {"bsp_name": "demo-bsp"}),
        ("start_hil_test", {"bsp_name": "demo-bsp", "backend": "lava"}),
        ("start_container_build", {}),
    ])
    async def test_routing_stored_in_extra(self, server_and_calls,
                                           tool_name, args):
        server, calls = server_and_calls
        await server.call_tool(tool_name, {
            **args, "notify_agent": "builder",
            "notify_session": "agent:main:s-1",
        })
        extra = calls[0][3]
        assert extra["notify_agent"] == "builder"
        assert extra["notify_session"] == "agent:main:s-1"

    async def test_no_routing_by_default(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_build", {"bsp_name": "demo-bsp"})
        assert calls[0][3] is None

    @pytest.mark.parametrize("value", ["bad value", "", "x" * 129, "a;b", "é"])
    async def test_invalid_agent_rejected(self, server_and_calls, value):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="notify_agent"):
            await server.call_tool("start_build", {
                "bsp_name": "demo-bsp", "notify_agent": value,
            })
        assert not calls

    @pytest.mark.parametrize("value", ["bad value", "a\nb"])
    async def test_invalid_session_rejected(self, server_and_calls, value):
        from bsp.mcp.server import ToolError
        server, calls = server_and_calls
        with pytest.raises(ToolError, match="notify_session"):
            await server.call_tool("start_build", {
                "bsp_name": "demo-bsp", "notify_session": value,
            })
        assert not calls

    async def test_existing_extra_preserved(self, server_and_calls):
        server, calls = server_and_calls
        await server.call_tool("start_scan", {
            "bsp_name": "demo-bsp", "notify_agent": "builder",
        })
        extra = calls[0][3]
        assert extra["reports_dir"] == "{job_dir}/reports"
        assert extra["notify_agent"] == "builder"


# ---------------------------------------------------------------------------
# End-to-end over stdio (needs a real registry)
# ---------------------------------------------------------------------------

REGISTRY_ENV = "BSP_TEST_REGISTRY"


def _is_error(result) -> bool:
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", False)
    return bool(flag)


def _text(result) -> str:
    return result.content[0].text


@pytest.mark.skipif(
    not os.environ.get(REGISTRY_ENV),
    reason=f"{REGISTRY_ENV} not set (must point at a bsp-registry.yml)",
)
class TestEndToEndStdio:
    async def test_tools_and_errors(self, tmp_path):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "bsp.mcp.server",
                "--registry", os.environ[REGISTRY_ENV],
                "--state-dir", str(tmp_path / "state"),
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "list_presets" in names
                assert "start_build" in names
                assert {"start_fetch", "start_deploy", "start_gather",
                        "start_flash", "start_container_build",
                        "list_containers", "registry_tree", "list_remotes",
                        "add_remote", "remove_remote", "rename_remote",
                        "set_remote_url", "wait_job"} <= names
                assert len(names) == 33

                prompts = await session.list_prompts()
                prompt_names = {p.name for p in prompts.prompts}
                assert {"smoke_test", "explore_registry", "validate_preset",
                        "build_and_triage", "scan_image",
                        "audit_layer_pins"} <= prompt_names
                prompt = await session.get_prompt(
                    "validate_preset", {"preset": "demo-bsp"}
                )
                text = prompt.messages[0].content.text
                assert "demo-bsp" in text
                assert "checkout_only" in text

                result = await session.call_tool("list_presets", {})
                assert not _is_error(result)

                bad = await session.call_tool("export_config", {"bsp_name": "--clean"})
                assert _is_error(bad)
                assert "Invalid bsp_name" in _text(bad)

                unknown = await session.call_tool(
                    "list_releases", {"device": "no-such-device-xyz"}
                )
                assert _is_error(unknown)
                assert "Unknown device" in _text(unknown)

                presets = json.loads(_text(result))
                if presets:
                    name = presets[0]["name"] if isinstance(presets, list) else presets["name"]
                    export = await session.call_tool("export_config", {"bsp_name": name})
                    if _is_error(export):
                        # Offline environments may not be able to resolve
                        # remote includes — the error must say why.
                        assert _text(export).strip()
                    else:
                        assert _text(export).strip()
