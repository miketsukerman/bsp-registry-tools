"""
Tests for tab-completion support: BspNameCompleter, new CLI commands (test, gather),
and the completions subcommand.
"""

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bsp
from bsp.completers import BspNameCompleter


# =============================================================================
# Helpers
# =============================================================================

def _make_parsed_args(**kwargs):
    """Return a SimpleNamespace-style object mirroring argparse Namespace."""
    defaults = dict(
        registry=None,
        local=False,
        remote="https://example.com/bsp-registry.git",
        branch="main",
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# =============================================================================
# BspNameCompleter tests
# =============================================================================

class TestBspNameCompleter:
    def test_returns_all_bsp_names(self, registry_file):
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(registry_file))
        result = completer("", parsed_args)
        assert "test-bsp" in result

    def test_filters_by_prefix(self, registry_file):
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(registry_file))
        result = completer("test", parsed_args)
        assert "test-bsp" in result

    def test_no_match_for_wrong_prefix(self, registry_file):
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(registry_file))
        result = completer("zzz-nonexistent", parsed_args)
        assert result == []

    def test_returns_empty_list_for_missing_registry(self, tmp_dir):
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(tmp_dir / "does-not-exist.yaml"))
        result = completer("", parsed_args)
        assert result == []

    def test_returns_empty_list_for_broken_registry(self, tmp_dir):
        broken = tmp_dir / "broken.yaml"
        broken.write_text("specification:\n  version: [invalid\n")
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(broken))
        result = completer("", parsed_args)
        assert result == []

    def test_uses_no_update_for_cached_remote(self, tmp_dir):
        """BspNameCompleter passes update=False to RegistryFetcher when falling back to remote."""
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args()  # no registry, no local, no local file

        with patch("bsp.completers.RegistryFetcher") as MockFetcher:
            instance = MockFetcher.return_value
            # Simulate that the fetcher raises SystemExit (no cache) — should return []
            instance.fetch_registry.side_effect = SystemExit(1)
            result = completer("", parsed_args)

        # fetch_registry must have been called with update=False
        instance.fetch_registry.assert_called_once()
        call_kwargs = instance.fetch_registry.call_args
        assert call_kwargs.kwargs.get("update") is False or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] is False
        )
        assert result == []

    def test_uses_local_file_when_present(self, tmp_dir, monkeypatch):
        """Completer discovers bsp-registry.yaml in the cwd when no --registry given."""
        monkeypatch.chdir(tmp_dir)
        registry_content = """
specification:
  version: "2.0"
containers:
  ubuntu-22.04:
    image: "test/ubuntu-22.04:latest"
    file: Dockerfile.ubuntu
    args: []
registry:
  devices:
    - slug: local-dev
      description: "Local Device"
      vendor: test-vendor
      soc_vendor: test-soc
      includes: []
  releases:
    - slug: local-rel
      description: "Local Release"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: local-bsp
      description: "BSP from local file"
      device: local-dev
      release: local-rel
      features: []
      build:
        container: "ubuntu-22.04"
        path: build/local
        configuration:
          - test.yml
"""
        (tmp_dir / "bsp-registry.yaml").write_text(registry_content)
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args()
        result = completer("", parsed_args)
        assert "local-bsp" in result

    def test_returns_multiple_bsp_names(self, tmp_dir):
        """Completer returns all matching names from a multi-BSP registry."""
        registry_content = """
specification:
  version: "2.0"
containers:
  ubuntu-22.04:
    image: "test/ubuntu-22.04:latest"
    file: Dockerfile.ubuntu
    args: []
registry:
  devices:
    - slug: dev-a
      description: "Device A"
      vendor: test-vendor
      soc_vendor: test-soc
      includes: []
  releases:
    - slug: rel-a
      description: "Release A"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: alpha-bsp
      description: "Alpha BSP"
      device: dev-a
      release: rel-a
      features: []
      build:
        container: "ubuntu-22.04"
        path: build/alpha
        configuration:
          - test.yml
    - name: beta-bsp
      description: "Beta BSP"
      device: dev-a
      release: rel-a
      features: []
      build:
        container: "ubuntu-22.04"
        path: build/beta
        configuration:
          - test.yml
"""
        registry_path = tmp_dir / "multi.yaml"
        registry_path.write_text(registry_content)
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(registry_path))
        result = completer("", parsed_args)
        assert set(result) == {"alpha-bsp", "beta-bsp"}

    def test_prefix_filter_returns_subset(self, tmp_dir):
        registry_content = """
specification:
  version: "2.0"
containers:
  ubuntu-22.04:
    image: "test/ubuntu-22.04:latest"
    file: Dockerfile.ubuntu
    args: []
registry:
  devices:
    - slug: dev-a
      description: "Device A"
      vendor: test-vendor
      soc_vendor: test-soc
      includes: []
  releases:
    - slug: rel-a
      description: "Release A"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: alpha-bsp
      description: "Alpha BSP"
      device: dev-a
      release: rel-a
      features: []
      build:
        container: "ubuntu-22.04"
        path: build/alpha
        configuration:
          - test.yml
    - name: beta-bsp
      description: "Beta BSP"
      device: dev-a
      release: rel-a
      features: []
      build:
        container: "ubuntu-22.04"
        path: build/beta
        configuration:
          - test.yml
"""
        registry_path = tmp_dir / "multi.yaml"
        registry_path.write_text(registry_content)
        completer = BspNameCompleter()
        parsed_args = _make_parsed_args(registry=str(registry_path))
        result = completer("alpha", parsed_args)
        assert result == ["alpha-bsp"]


# =============================================================================
# CLI smoke tests — argparse parsing for new subcommands
# =============================================================================

class TestNewSubcommandParsing:
    def test_test_command_parses_bsp_name(self, registry_file):
        from bsp.bsp_manager import BspManager
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "test", "test-bsp"]):
            with patch.object(BspManager, "test_bsp", side_effect=NotImplementedError) as mock_test:
                exit_code = bsp.main()
        # Should fail (NotImplementedError → exit 1) but the parse must succeed
        mock_test.assert_called_once_with(
            "test-bsp",
            lava_server=None,
            lava_token=None,
            artifact_url=None,
            wait=False,
        )
        assert exit_code != 0

    def test_test_command_parses_wait_flag(self, registry_file):
        from bsp.bsp_manager import BspManager
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "test", "test-bsp", "--wait"
        ]):
            with patch.object(BspManager, "test_bsp", side_effect=NotImplementedError) as mock_test:
                exit_code = bsp.main()
        _, kwargs = mock_test.call_args
        assert kwargs.get("wait") is True
        assert exit_code != 0

    def test_gather_command_parses_bsp_name(self, registry_file):
        from bsp.bsp_manager import BspManager
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "gather", "test-bsp"]):
            with patch.object(BspManager, "gather_bsp", side_effect=NotImplementedError) as mock_gather:
                exit_code = bsp.main()
        mock_gather.assert_called_once()
        args, kwargs = mock_gather.call_args
        assert args[0] == "test-bsp"
        assert exit_code != 0

    def test_gather_command_parses_dest_dir_flag(self, registry_file, tmp_dir):
        from bsp.bsp_manager import BspManager
        output = str(tmp_dir / "artifacts")
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "gather", "test-bsp", "--dest-dir", output
        ]):
            with patch.object(BspManager, "gather_bsp", side_effect=NotImplementedError) as mock_gather:
                exit_code = bsp.main()
        mock_gather.assert_called_once()
        _, kwargs = mock_gather.call_args
        assert kwargs.get("dest_dir") == output
        assert exit_code != 0


# =============================================================================
# completions subcommand
# =============================================================================

class TestCompletionsSubcommand:
    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "tcsh"])
    def test_completions_prints_nonempty_script(self, shell, capsys):
        with patch("sys.argv", ["bsp", "completions", shell]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert len(captured.out.strip()) > 0

    def test_completions_invalid_shell_exits_nonzero(self):
        with patch("sys.argv", ["bsp", "completions", "powershell"]):
            exit_code = bsp.main()
        assert exit_code != 0
