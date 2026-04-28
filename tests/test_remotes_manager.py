"""
Tests for RemotesManager and the ``bsp remotes`` CLI sub-command.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bsp.remotes_manager import RemotesManager, RemoteEntry, DEFAULT_REMOTES_CONFIG


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def remotes_config(tmp_path) -> Path:
    """Return a temp path for the remotes config file and set BSP_REMOTES_CONFIG."""
    cfg = tmp_path / "remotes.yaml"
    with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(cfg)}):
        yield cfg


@pytest.fixture
def mgr(remotes_config) -> RemotesManager:
    """A RemotesManager backed by a temp config file (via env var)."""
    return RemotesManager(config_path=remotes_config)


# ---------------------------------------------------------------------------
# RemotesManager unit tests
# ---------------------------------------------------------------------------

class TestRemotesManagerLoad:
    def test_load_empty_when_no_file(self, tmp_path):
        mgr = RemotesManager(config_path=tmp_path / "nonexistent.yaml")
        assert mgr.load() == []

    def test_load_returns_entries(self, tmp_path):
        cfg = tmp_path / "remotes.yaml"
        cfg.write_text(yaml.safe_dump({
            "remotes": [
                {"name": "origin", "url": "https://example.com/registry.git", "branch": "main"},
            ]
        }))
        mgr = RemotesManager(config_path=cfg)
        entries = mgr.load()
        assert len(entries) == 1
        assert entries[0].name == "origin"
        assert entries[0].url == "https://example.com/registry.git"
        assert entries[0].branch == "main"

    def test_load_skips_invalid_entries(self, tmp_path):
        cfg = tmp_path / "remotes.yaml"
        cfg.write_text(yaml.safe_dump({
            "remotes": [
                {"name": "good", "url": "https://example.com/a.git"},
                {"url": "https://example.com/b.git"},   # no name — should be skipped
                "not-a-dict",                           # not a dict — should be skipped
            ]
        }))
        mgr = RemotesManager(config_path=cfg)
        entries = mgr.load()
        assert len(entries) == 1
        assert entries[0].name == "good"


class TestRemotesManagerAdd:
    def test_add_creates_file(self, mgr, remotes_config):
        mgr.add("myremote", "https://example.com/registry.git")
        assert remotes_config.is_file()

    def test_add_stores_entry(self, mgr):
        mgr.add("alpha", "https://example.com/a.git", branch="develop")
        entries = mgr.load()
        assert len(entries) == 1
        assert entries[0].name == "alpha"
        assert entries[0].url == "https://example.com/a.git"
        assert entries[0].branch == "develop"

    def test_add_multiple_entries(self, mgr):
        mgr.add("a", "https://example.com/a.git")
        mgr.add("b", "https://example.com/b.git")
        entries = mgr.load()
        assert [e.name for e in entries] == ["a", "b"]

    def test_add_duplicate_name_exits(self, mgr):
        mgr.add("dup", "https://example.com/dup.git")
        with pytest.raises(SystemExit):
            mgr.add("dup", "https://example.com/other.git")

    def test_add_returns_entry(self, mgr):
        entry = mgr.add("r", "https://example.com/r.git")
        assert isinstance(entry, RemoteEntry)
        assert entry.name == "r"


class TestRemotesManagerRemove:
    def test_remove_deletes_entry(self, mgr):
        mgr.add("a", "https://example.com/a.git")
        mgr.add("b", "https://example.com/b.git")
        mgr.remove("a")
        entries = mgr.load()
        assert [e.name for e in entries] == ["b"]

    def test_remove_nonexistent_exits(self, mgr):
        with pytest.raises(SystemExit):
            mgr.remove("ghost")


class TestRemotesManagerRename:
    def test_rename_changes_name(self, mgr):
        mgr.add("old", "https://example.com/old.git")
        mgr.rename("old", "new")
        entries = mgr.load()
        assert entries[0].name == "new"
        assert entries[0].url == "https://example.com/old.git"

    def test_rename_nonexistent_exits(self, mgr):
        with pytest.raises(SystemExit):
            mgr.rename("ghost", "new")

    def test_rename_to_existing_name_exits(self, mgr):
        mgr.add("a", "https://example.com/a.git")
        mgr.add("b", "https://example.com/b.git")
        with pytest.raises(SystemExit):
            mgr.rename("a", "b")


class TestRemotesManagerSetUrl:
    def test_set_url_updates_url(self, mgr):
        mgr.add("r", "https://example.com/old.git")
        mgr.set_url("r", "https://example.com/new.git")
        assert mgr.load()[0].url == "https://example.com/new.git"

    def test_set_url_nonexistent_exits(self, mgr):
        with pytest.raises(SystemExit):
            mgr.set_url("ghost", "https://example.com/ghost.git")


class TestRemotesManagerSetBranch:
    def test_set_branch_updates_branch(self, mgr):
        mgr.add("r", "https://example.com/r.git", branch="main")
        mgr.set_branch("r", "develop")
        assert mgr.load()[0].branch == "develop"


class TestRemotesManagerGet:
    def test_get_returns_entry(self, mgr):
        mgr.add("foo", "https://example.com/foo.git")
        entry = mgr.get("foo")
        assert entry.name == "foo"

    def test_get_nonexistent_exits(self, mgr):
        with pytest.raises(SystemExit):
            mgr.get("ghost")


# ---------------------------------------------------------------------------
# CLI integration tests for ``bsp remotes``
# ---------------------------------------------------------------------------

class TestRemotesCliAdd:
    def test_add_prints_confirmation(self, remotes_config, capsys):
        import bsp
        with patch("sys.argv", ["bsp", "remotes", "add", "origin", "https://example.com/a.git"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "origin" in out

    def test_add_persists_entry(self, remotes_config):
        import bsp
        with patch("sys.argv", ["bsp", "remotes", "add", "origin", "https://example.com/a.git",
                                 "--branch", "develop"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                bsp.main()
        mgr = RemotesManager(config_path=remotes_config)
        entries = mgr.load()
        assert len(entries) == 1
        assert entries[0].branch == "develop"


class TestRemotesCliList:
    def test_list_shows_names(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("alpha", "https://example.com/a.git")
        RemotesManager(config_path=remotes_config).add("beta", "https://example.com/b.git")
        with patch("sys.argv", ["bsp", "remotes"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_list_verbose_shows_urls(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("r", "https://example.com/r.git")
        with patch("sys.argv", ["bsp", "remotes", "-v"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                bsp.main()
        out = capsys.readouterr().out
        assert "https://example.com/r.git" in out

    def test_list_empty_prints_help(self, remotes_config, capsys):
        import bsp
        with patch("sys.argv", ["bsp", "remotes"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "no remotes" in out or "bsp remotes add" in out


class TestRemotesCliRemove:
    def test_remove_deletes_entry(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("r", "https://example.com/r.git")
        with patch("sys.argv", ["bsp", "remotes", "remove", "r"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        assert RemotesManager(config_path=remotes_config).load() == []


class TestRemotesCliRename:
    def test_rename_changes_name(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("old", "https://example.com/r.git")
        with patch("sys.argv", ["bsp", "remotes", "rename", "old", "new"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        entries = RemotesManager(config_path=remotes_config).load()
        assert entries[0].name == "new"


class TestRemotesCliSetUrl:
    def test_set_url_updates(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("r", "https://example.com/old.git")
        with patch("sys.argv", ["bsp", "remotes", "set-url", "r", "https://example.com/new.git"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        assert RemotesManager(config_path=remotes_config).load()[0].url == "https://example.com/new.git"


class TestRemotesCliShow:
    def test_show_prints_details(self, remotes_config, capsys):
        import bsp
        RemotesManager(config_path=remotes_config).add("r", "https://example.com/r.git", branch="dev")
        with patch("sys.argv", ["bsp", "remotes", "show", "r"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                exit_code = bsp.main()
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "https://example.com/r.git" in out
        assert "dev" in out


class TestRemotesIntegrationWithRegistryLoad:
    """When no local registry and no --remote flag, stored remotes should be used."""

    def test_stored_single_remote_is_fetched(self, tmp_path, remotes_config):
        """A single stored remote is fetched via fetch_registry."""
        import bsp
        RemotesManager(config_path=remotes_config).add(
            "custom", "https://example.com/custom.git", branch="dev"
        )

        fake_registry = tmp_path / "bsp-registry.yaml"
        fake_registry.write_text("specification:\n  version: '2.0'\nregistry: {}\n")

        # Patch only the local-registry lookup (not Path.is_file globally)
        with patch("sys.argv", ["bsp", "list"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                # Make local_registry detection return None by patching
                # the specific filenames check in cli
                with patch("bsp.cli.Path") as MockPath:
                    mock_path_instance = MockPath.return_value
                    mock_path_instance.is_file.return_value = False
                    MockPath.side_effect = lambda s: Path(s)  # pass-through for other uses

                    # But we want registry is None (no --registry flag) so go to else branch
                    with patch(
                        "bsp.registry_fetcher.RegistryFetcher.fetch_registry",
                        return_value=fake_registry,
                    ) as mock_fetch:
                        with patch("bsp.cli.RemotesManager") as MockRM:
                            from bsp.remotes_manager import RemoteEntry
                            MockRM.return_value.load.return_value = [
                                RemoteEntry(
                                    name="custom",
                                    url="https://example.com/custom.git",
                                    branch="dev",
                                )
                            ]
                            bsp.main()

        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args[1]
        assert call_kwargs["repo_url"] == "https://example.com/custom.git"
        assert call_kwargs["branch"] == "dev"

    def test_stored_multiple_remotes_use_fetch_multiple(self, tmp_path, remotes_config):
        """Multiple stored remotes dispatch to fetch_multiple."""
        import bsp

        fake_registry = tmp_path / "bsp-registry.yaml"
        fake_registry.write_text("specification:\n  version: '2.0'\nregistry: {}\n")

        with patch("sys.argv", ["bsp", "list"]):
            with patch.dict(os.environ, {"BSP_REMOTES_CONFIG": str(remotes_config)}):
                with patch(
                    "bsp.registry_fetcher.RegistryFetcher.fetch_multiple",
                    return_value=[("a", fake_registry), ("b", fake_registry)],
                ) as mock_fetch:
                    with patch("bsp.bsp_manager.BspManager.initialize"):
                        with patch("bsp.cli.RemotesManager") as MockRM:
                            from bsp.remotes_manager import RemoteEntry
                            MockRM.return_value.load.return_value = [
                                RemoteEntry(name="a", url="https://example.com/a.git"),
                                RemoteEntry(name="b", url="https://example.com/b.git"),
                            ]
                            bsp.main()

        mock_fetch.assert_called_once()
        specs = mock_fetch.call_args[0][0]
        assert len(specs) == 2
        assert specs[0].name == "a"
        assert specs[1].name == "b"
