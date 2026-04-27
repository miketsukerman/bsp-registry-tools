"""
Tests for the --remote, --branch, --update/--no-update, and --local CLI flags.
"""

from unittest.mock import patch

import bsp
from bsp.registry_fetcher import DEFAULT_BRANCH, DEFAULT_REMOTE_URL


class TestMainCliRemoteFlags:
    """Tests covering the new --remote, --branch, --update/--no-update, and --local flags."""

    def test_default_remote_url_passed_to_fetcher(self, registry_file):
        """When no local registry exists, the default remote URL is forwarded to RegistryFetcher."""
        with patch("sys.argv", ["bsp", "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=DEFAULT_REMOTE_URL,
            branch=DEFAULT_BRANCH,
            update=True,
        )
        assert exit_code == 0

    def test_custom_remote_url_passed_to_fetcher(self, registry_file):
        """--remote flag forwards a custom URL to RegistryFetcher."""
        custom_url = "https://github.com/my-org/bsp-registry.git"
        with patch("sys.argv", ["bsp", "--remote", custom_url, "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=custom_url,
            branch=DEFAULT_BRANCH,
            update=True,
        )
        assert exit_code == 0

    def test_custom_branch_passed_to_fetcher(self, registry_file):
        """--branch flag forwards a custom branch name to RegistryFetcher."""
        with patch("sys.argv", ["bsp", "--branch", "dev", "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=DEFAULT_REMOTE_URL,
            branch="dev",
            update=True,
        )
        assert exit_code == 0

    def test_no_update_flag_passes_update_false_to_fetcher(self, registry_file):
        """--no-update passes update=False to RegistryFetcher."""
        with patch("sys.argv", ["bsp", "--no-update", "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=DEFAULT_REMOTE_URL,
            branch=DEFAULT_BRANCH,
            update=False,
        )
        assert exit_code == 0

    def test_update_flag_passes_update_true_to_fetcher(self, registry_file):
        """--update (explicit) passes update=True to RegistryFetcher."""
        with patch("sys.argv", ["bsp", "--update", "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=DEFAULT_REMOTE_URL,
            branch=DEFAULT_BRANCH,
            update=True,
        )
        assert exit_code == 0

    def test_local_flag_skips_fetcher(self, registry_file):
        """--local skips RegistryFetcher and uses the local bsp-registry.yaml path."""
        with patch("sys.argv", ["bsp", "--local", "--registry", str(registry_file), "list"]):
            with patch(
                "bsp.registry_fetcher.RegistryFetcher.fetch_registry"
            ) as mock_fetch:
                exit_code = bsp.main()

        mock_fetch.assert_not_called()
        assert exit_code == 0

    def test_explicit_registry_skips_fetcher(self, registry_file):
        """--registry <path> skips RegistryFetcher regardless of other flags."""
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list"]):
            with patch(
                "bsp.registry_fetcher.RegistryFetcher.fetch_registry"
            ) as mock_fetch:
                exit_code = bsp.main()

        mock_fetch.assert_not_called()
        assert exit_code == 0

    def test_local_bsp_registry_yaml_in_cwd_skips_fetcher(self, registry_file, tmp_dir, monkeypatch):
        """When bsp-registry.yaml exists in the CWD, remote fetch is skipped."""
        # Change working directory so the file is auto-detected
        monkeypatch.chdir(tmp_dir)
        with patch("sys.argv", ["bsp", "list"]):
            with patch(
                "bsp.registry_fetcher.RegistryFetcher.fetch_registry"
            ) as mock_fetch:
                exit_code = bsp.main()

        mock_fetch.assert_not_called()
        assert exit_code == 0

    def test_local_bsp_registry_yml_in_cwd_skips_fetcher(self, tmp_dir, monkeypatch):
        """When bsp-registry.yml (no .yaml) exists in the CWD, remote fetch is skipped."""
        from tests.conftest import MINIMAL_REGISTRY_YAML
        registry_yml = tmp_dir / "bsp-registry.yml"
        registry_yml.write_text(MINIMAL_REGISTRY_YAML)
        monkeypatch.chdir(tmp_dir)
        with patch("sys.argv", ["bsp", "list"]):
            with patch(
                "bsp.registry_fetcher.RegistryFetcher.fetch_registry"
            ) as mock_fetch:
                exit_code = bsp.main()

        mock_fetch.assert_not_called()
        assert exit_code == 0

    def test_yaml_extension_takes_priority_over_yml(self, tmp_dir, monkeypatch, capsys):
        """When both bsp-registry.yaml and bsp-registry.yml exist, .yaml is preferred."""
        from tests.conftest import MINIMAL_REGISTRY_YAML
        # Write different BSP names to distinguish which file was loaded
        yaml_content = MINIMAL_REGISTRY_YAML.replace("test-bsp", "bsp-from-yaml")
        yml_content = MINIMAL_REGISTRY_YAML.replace("test-bsp", "bsp-from-yml")
        (tmp_dir / "bsp-registry.yaml").write_text(yaml_content)
        (tmp_dir / "bsp-registry.yml").write_text(yml_content)
        monkeypatch.chdir(tmp_dir)
        with patch("sys.argv", ["bsp", "list"]):
            exit_code = bsp.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "bsp-from-yaml" in captured.out
        assert "bsp-from-yml" not in captured.out

    def test_local_flag_uses_yml_when_only_yml_exists(self, tmp_dir, monkeypatch, capsys):
        """--local uses bsp-registry.yml when only that file is present."""
        from tests.conftest import MINIMAL_REGISTRY_YAML
        registry_yml = tmp_dir / "bsp-registry.yml"
        registry_yml.write_text(MINIMAL_REGISTRY_YAML)
        monkeypatch.chdir(tmp_dir)
        with patch("sys.argv", ["bsp", "--local", "list"]):
            with patch(
                "bsp.registry_fetcher.RegistryFetcher.fetch_registry"
            ) as mock_fetch:
                exit_code = bsp.main()

        mock_fetch.assert_not_called()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-bsp" in captured.out

    def test_remote_and_branch_combined(self, registry_file):
        """--remote and --branch together are both forwarded to RegistryFetcher."""
        custom_url = "https://github.com/corp/registry.git"
        with patch("sys.argv", ["bsp", "--remote", custom_url, "--branch", "release", "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once_with(
            repo_url=custom_url,
            branch="release",
            update=True,
        )
        assert exit_code == 0


class TestMultiRemoteCliFlags:
    """Tests for multi-registry --remote CLI behavior."""

    def test_multiple_remotes_dispatch_to_fetch_multiple(self, registry_file):
        """Two --remote flags should dispatch to RegistryFetcher.fetch_multiple."""
        url_a = "https://github.com/org/registry-a.git"
        url_b = "https://github.com/org/registry-b.git"
        with patch("sys.argv", ["bsp", "--remote", url_a, "--remote", url_b, "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_multiple",
                    return_value=[("a", registry_file), ("b", registry_file)],
                ) as mock_fetch:
                    with patch("bsp.bsp_manager.BspManager.initialize"):
                        bsp.main()

        mock_fetch.assert_called_once()
        specs = mock_fetch.call_args[0][0]
        assert len(specs) == 2
        assert specs[0].url == url_a
        assert specs[1].url == url_b

    def test_multiple_remotes_use_config_paths_on_manager(self, registry_file):
        """With two --remote flags, BspManager should be created with config_paths."""
        url_a = "https://github.com/org/registry-a.git"
        url_b = "https://github.com/org/registry-b.git"
        created_managers = []

        original_init = bsp.BspManager.__init__

        def capturing_init(self_inner, *args, **kwargs):
            created_managers.append(kwargs)
            original_init(self_inner, *args, **kwargs)

        with patch("sys.argv", ["bsp", "--remote", url_a, "--remote", url_b, "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_multiple",
                    return_value=[("a", registry_file), ("b", registry_file)],
                ):
                    with patch.object(bsp.BspManager, "__init__", capturing_init):
                        with patch.object(bsp.BspManager, "initialize"):
                            bsp.main()

        assert any("config_paths" in kw for kw in created_managers)

    def test_single_remote_with_branch_embedded(self, registry_file):
        """Single --remote URL@BRANCH should parse correctly."""
        url_and_branch = "https://github.com/org/registry.git@my-branch"
        with patch("sys.argv", ["bsp", "--remote", url_and_branch, "list"]):
            with patch("pathlib.Path.is_file", return_value=False):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                    return_value=registry_file,
                ) as mock_fetch:
                    exit_code = bsp.main()

        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args[1]
        assert call_kwargs["repo_url"] == "https://github.com/org/registry.git"
        assert call_kwargs["branch"] == "my-branch"
        assert exit_code == 0
