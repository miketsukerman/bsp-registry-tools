"""
Tests for the ArtifactGatherer and related cloud-storage download functionality.

Covers:
- GatherResult dataclass
- ArtifactGatherer.compose_remote_prefix
- ArtifactGatherer.gather (manifest-guided and fallback listing paths)
- CloudStorageBackend.get_manifest (via a fake backend)
- CloudStorageBackend.download_prefix (via a fake backend)
- AzureStorageBackend.download_file (mocked SDK)
- AwsStorageBackend.download_file (mocked SDK)
- CLI gather command argument parsing
- BspManager.gather_bsp / gather_by_components (mocked)
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from bsp.models import DeployConfig
from bsp.gatherer import ArtifactGatherer, GatherResult
from bsp.storage.base import CloudStorageBackend
from bsp.storage import create_backend


# =============================================================================
# Fake backend for unit testing
# =============================================================================


class _FakeBackend(CloudStorageBackend):
    """
    In-memory backend for testing ArtifactGatherer without touching cloud APIs.

    Blobs are stored as ``remote_path → bytes`` in :attr:`blobs`.
    """

    def __init__(self, dry_run=False):
        super().__init__(dry_run=dry_run)
        self.blobs: dict = {}          # remote_path → bytes content
        self.downloaded: list = []     # (remote_path, local_path) tuples

    def upload_file(self, local_path, remote_path):
        if self.dry_run:
            return f"dry-run:{remote_path}"
        with open(local_path, "rb") as fh:
            self.blobs[remote_path] = fh.read()
        return f"fake://{remote_path}"

    def download_file(self, remote_path, local_path):
        local_path = Path(local_path)
        if self.dry_run:
            return
        if remote_path not in self.blobs:
            raise FileNotFoundError(f"Blob not found: {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.blobs[remote_path])
        self.downloaded.append((remote_path, local_path))

    def list_artifacts(self, remote_prefix):
        if self.dry_run:
            return []
        return [k for k in self.blobs if k.startswith(remote_prefix)]

    def get_upload_url(self, remote_path):
        return f"fake://{remote_path}"


# =============================================================================
# GatherResult tests
# =============================================================================


class TestGatherResult:
    def test_defaults(self):
        result = GatherResult()
        assert result.artifacts == []
        assert result.manifest is None
        assert result.dest_dir is None
        assert result.dry_run is False

    def test_total_count_empty(self):
        assert GatherResult().total_count == 0

    def test_total_count_with_artifacts(self, tmp_path):
        p1 = tmp_path / "a.wic.gz"
        p2 = tmp_path / "b.wic.gz"
        result = GatherResult(artifacts=[p1, p2])
        assert result.total_count == 2


# =============================================================================
# ArtifactGatherer.compose_remote_prefix tests
# =============================================================================


class TestComposeRemotePrefix:
    def _make_gatherer(self, **cfg_kwargs):
        return ArtifactGatherer(DeployConfig(**cfg_kwargs), _FakeBackend())

    def test_default_template_uses_today(self):
        import datetime
        g = self._make_gatherer()
        prefix = g.compose_remote_prefix(device="mydev", release="myrel", vendor="myvendor")
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        assert today in prefix
        assert "mydev" in prefix
        assert "myrel" in prefix
        assert "myvendor" in prefix

    def test_custom_prefix_template(self):
        g = self._make_gatherer(prefix="{device}/{release}")
        prefix = g.compose_remote_prefix(device="rpi5", release="scarthgap")
        assert prefix == "rpi5/scarthgap"

    def test_date_override(self):
        g = self._make_gatherer(prefix="{device}/{date}")
        prefix = g.compose_remote_prefix(device="rpi5", date_override="2024-01-15")
        assert prefix == "rpi5/2024-01-15"

    def test_strips_leading_trailing_slashes(self):
        g = self._make_gatherer(prefix="/{device}/")
        prefix = g.compose_remote_prefix(device="rpi5")
        assert not prefix.startswith("/")
        assert not prefix.endswith("/")

    def test_unknown_placeholders_filled(self):
        g = self._make_gatherer(prefix="{vendor}/{device}/{release}/{date}")
        prefix = g.compose_remote_prefix()
        assert "unknown" in prefix


# =============================================================================
# CloudStorageBackend.get_manifest tests
# =============================================================================


class TestGetManifest:
    def test_returns_none_when_blob_missing(self, tmp_path):
        backend = _FakeBackend()
        result = backend.get_manifest("myvendor/mydev/myrel/2024-01-01")
        assert result is None

    def test_parses_manifest_json(self, tmp_path):
        backend = _FakeBackend()
        manifest_data = {
            "schema_version": "1",
            "artifacts": [{"name": "image.wic.gz", "size_bytes": 1024, "sha256": "abc"}],
        }
        prefix = "myvendor/mydev/myrel/2024-01-01"
        backend.blobs[f"{prefix}/manifest.json"] = json.dumps(manifest_data).encode()

        result = backend.get_manifest(prefix)
        assert result is not None
        assert result["schema_version"] == "1"
        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0]["name"] == "image.wic.gz"

    def test_returns_none_on_invalid_json(self, tmp_path):
        backend = _FakeBackend()
        prefix = "some/prefix"
        backend.blobs[f"{prefix}/manifest.json"] = b"not-valid-json{"

        result = backend.get_manifest(prefix)
        assert result is None

    def test_dry_run_returns_none(self):
        backend = _FakeBackend(dry_run=True)
        result = backend.get_manifest("some/prefix")
        assert result is None


# =============================================================================
# CloudStorageBackend.download_prefix tests
# =============================================================================


class TestDownloadPrefix:
    def test_downloads_all_blobs(self, tmp_path):
        backend = _FakeBackend()
        prefix = "vendor/device/rel/2024-01-01"
        backend.blobs[f"{prefix}/image.wic.gz"] = b"wic_data"
        backend.blobs[f"{prefix}/image.tar.gz"] = b"tar_data"

        downloaded = backend.download_prefix(prefix, tmp_path)
        assert len(downloaded) == 2
        names = {p.name for p in downloaded}
        assert "image.wic.gz" in names
        assert "image.tar.gz" in names

    def test_skips_manifest_json(self, tmp_path):
        backend = _FakeBackend()
        prefix = "vendor/device/rel/2024-01-01"
        backend.blobs[f"{prefix}/image.wic.gz"] = b"data"
        backend.blobs[f"{prefix}/manifest.json"] = b"{}"

        downloaded = backend.download_prefix(prefix, tmp_path)
        names = {p.name for p in downloaded}
        assert "manifest.json" not in names
        assert "image.wic.gz" in names

    def test_dry_run_returns_empty(self, tmp_path):
        backend = _FakeBackend(dry_run=True)
        result = backend.download_prefix("some/prefix", tmp_path)
        assert result == []

    def test_creates_dest_dir(self, tmp_path):
        backend = _FakeBackend()
        new_dir = tmp_path / "new" / "dir"
        backend.download_prefix("empty/prefix", new_dir)
        assert new_dir.is_dir()


# =============================================================================
# ArtifactGatherer.gather tests
# =============================================================================


class TestArtifactGathererGather:
    def _make_gatherer_with_artifacts(self, tmp_path):
        """Return a gatherer whose backend contains two blobs and a manifest."""
        backend = _FakeBackend()
        prefix = "vendor/mydev/myrel/2024-01-15"
        backend.blobs[f"{prefix}/image.wic.gz"] = b"wic_data"
        backend.blobs[f"{prefix}/image.tar.gz"] = b"tar_data"
        manifest = {
            "schema_version": "1",
            "artifacts": [
                {"name": "image.wic.gz"},
                {"name": "image.tar.gz"},
            ],
        }
        backend.blobs[f"{prefix}/manifest.json"] = json.dumps(manifest).encode()

        cfg = DeployConfig(
            provider="azure",
            container="my-container",
            prefix="{vendor}/{device}/{release}/{date}",
        )
        return ArtifactGatherer(cfg, backend), backend, prefix

    def test_gather_uses_manifest_when_available(self, tmp_path):
        gatherer, backend, prefix = self._make_gatherer_with_artifacts(tmp_path)
        result = gatherer.gather(
            dest_dir=str(tmp_path),
            device="mydev",
            release="myrel",
            vendor="vendor",
            date_override="2024-01-15",
        )
        assert result.manifest is not None
        assert result.total_count == 2
        names = {p.name for p in result.artifacts}
        assert "image.wic.gz" in names
        assert "image.tar.gz" in names

    def test_gather_falls_back_to_list_when_no_manifest(self, tmp_path):
        backend = _FakeBackend()
        prefix = "vendor/mydev/myrel/2024-01-15"
        backend.blobs[f"{prefix}/image.wic.gz"] = b"wic_data"

        cfg = DeployConfig(prefix="{vendor}/{device}/{release}/{date}")
        gatherer = ArtifactGatherer(cfg, backend)
        result = gatherer.gather(
            dest_dir=str(tmp_path),
            device="mydev",
            release="myrel",
            vendor="vendor",
            date_override="2024-01-15",
        )
        assert result.manifest is None
        assert result.total_count == 1
        assert result.artifacts[0].name == "image.wic.gz"

    def test_gather_dry_run_returns_empty_result(self, tmp_path):
        backend = _FakeBackend(dry_run=True)
        cfg = DeployConfig(prefix="{vendor}/{device}/{release}/{date}")
        gatherer = ArtifactGatherer(cfg, backend)
        result = gatherer.gather(
            dest_dir=str(tmp_path),
            device="mydev",
            release="myrel",
            vendor="vendor",
            date_override="2024-01-15",
        )
        assert result.dry_run is True
        assert result.total_count == 0

    def test_gather_result_dest_dir_is_set(self, tmp_path):
        gatherer, _, _ = self._make_gatherer_with_artifacts(tmp_path)
        result = gatherer.gather(
            dest_dir=str(tmp_path),
            device="mydev",
            release="myrel",
            vendor="vendor",
            date_override="2024-01-15",
        )
        assert result.dest_dir == tmp_path

    def test_gather_writes_files_to_dest(self, tmp_path):
        gatherer, _, _ = self._make_gatherer_with_artifacts(tmp_path)
        gatherer.gather(
            dest_dir=str(tmp_path),
            device="mydev",
            release="myrel",
            vendor="vendor",
            date_override="2024-01-15",
        )
        assert (tmp_path / "image.wic.gz").exists()
        assert (tmp_path / "image.tar.gz").exists()


# =============================================================================
# AzureStorageBackend.download_file tests
# =============================================================================


class TestAzureStorageBackendDownload:
    def _make_azure_backend(self, container="test-container"):
        """Return an AzureStorageBackend wired to a mocked _client."""
        try:
            from bsp.storage.azure import AzureStorageBackend
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

        backend = AzureStorageBackend(container_name=container, dry_run=True)
        # Replace the dry_run flag and inject a mock client so we can test real paths
        backend.dry_run = False
        mock_client = MagicMock()
        backend._client = mock_client
        return backend, mock_client

    def test_download_file_calls_readinto(self, tmp_path):
        try:
            from bsp.storage.azure import AzureStorageBackend
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

        backend, mock_client = self._make_azure_backend()
        mock_blob_client = MagicMock()
        mock_stream = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.download_blob.return_value = mock_stream

        dest = tmp_path / "image.wic.gz"
        backend.download_file("vendor/device/rel/date/image.wic.gz", dest)

        mock_client.get_blob_client.assert_called_once_with(
            container="test-container", blob="vendor/device/rel/date/image.wic.gz"
        )
        mock_blob_client.download_blob.assert_called_once()
        mock_stream.readinto.assert_called_once()

    def test_download_file_dry_run_skips_api(self, tmp_path):
        try:
            from bsp.storage.azure import AzureStorageBackend
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

        backend = AzureStorageBackend(container_name="test", dry_run=True)
        dest = tmp_path / "image.wic.gz"
        backend.download_file("some/remote/image.wic.gz", dest)
        # File should NOT have been created
        assert not dest.exists()

    def test_download_file_creates_parent_dirs(self, tmp_path):
        try:
            from bsp.storage.azure import AzureStorageBackend
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

        backend, mock_client = self._make_azure_backend()
        mock_blob_client = MagicMock()
        mock_stream = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.download_blob.return_value = mock_stream

        deep_dest = tmp_path / "a" / "b" / "c" / "image.wic.gz"
        backend.download_file("some/key", deep_dest)
        assert deep_dest.parent.is_dir()


# =============================================================================
# AwsStorageBackend.download_file tests
# =============================================================================


class TestAwsStorageBackendDownload:
    def _make_aws_backend(self, bucket="test-bucket"):
        try:
            from bsp.storage.aws import AwsStorageBackend
        except ImportError:
            pytest.skip("boto3 not installed")

        backend = AwsStorageBackend(bucket_name=bucket, dry_run=True)
        backend.dry_run = False
        mock_s3 = MagicMock()
        backend._s3 = mock_s3
        return backend, mock_s3

    def test_download_file_calls_s3_download(self, tmp_path):
        try:
            from bsp.storage.aws import AwsStorageBackend
        except ImportError:
            pytest.skip("boto3 not installed")

        backend, mock_s3 = self._make_aws_backend()
        dest = tmp_path / "image.wic.gz"
        backend.download_file("vendor/device/rel/date/image.wic.gz", dest)

        mock_s3.download_file.assert_called_once_with(
            "test-bucket",
            "vendor/device/rel/date/image.wic.gz",
            str(dest),
        )

    def test_download_file_dry_run_skips_api(self, tmp_path):
        try:
            from bsp.storage.aws import AwsStorageBackend
        except ImportError:
            pytest.skip("boto3 not installed")

        backend = AwsStorageBackend(bucket_name="test", dry_run=True)
        dest = tmp_path / "image.wic.gz"
        backend.download_file("some/key", dest)
        assert not dest.exists()

    def test_download_file_creates_parent_dirs(self, tmp_path):
        try:
            from bsp.storage.aws import AwsStorageBackend
        except ImportError:
            pytest.skip("boto3 not installed")

        backend, mock_s3 = self._make_aws_backend()
        deep_dest = tmp_path / "a" / "b" / "image.wic.gz"
        backend.download_file("some/key", deep_dest)
        assert deep_dest.parent.is_dir()


# =============================================================================
# CLI gather command argument tests
# =============================================================================


class TestCliGatherArguments:
    def _run_cli(self, args, registry_path):
        from bsp.cli import main as cli_main
        with patch("sys.argv", ["bsp", "--registry", str(registry_path)] + args):
            with patch("bsp.bsp_manager.BspManager.gather_bsp") as mock_gather:
                mock_gather.return_value = GatherResult()
                with patch("bsp.bsp_manager.BspManager.initialize"):
                    with patch("bsp.bsp_manager.BspManager.load_configuration"):
                        with patch("bsp.bsp_manager.BspManager.cleanup"):
                            return cli_main(), mock_gather

    def test_gather_bsp_name(self, tmp_path):
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text("specification:\n  version: '2.0'\nregistry:\n  devices: []\n  releases: []\n")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "gather", "my-preset"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.gather_bsp.return_value = GatherResult()
                from bsp.cli import main as cli_main
                cli_main()
                mock_mgr.gather_bsp.assert_called_once()
                call_kwargs = mock_mgr.gather_bsp.call_args
                assert call_kwargs[0][0] == "my-preset" or call_kwargs[1].get("bsp_name") == "my-preset" or "my-preset" in str(call_kwargs)

    def test_gather_dry_run_flag(self, tmp_path):
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text("specification:\n  version: '2.0'\nregistry:\n  devices: []\n  releases: []\n")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "gather", "my-preset", "--dry-run"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.gather_bsp.return_value = GatherResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.gather_bsp.call_args
                assert kwargs.get("dry_run") is True

    def test_gather_date_flag(self, tmp_path):
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text("specification:\n  version: '2.0'\nregistry:\n  devices: []\n  releases: []\n")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "gather", "my-preset", "--date", "2024-01-15"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.gather_bsp.return_value = GatherResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.gather_bsp.call_args
                assert kwargs.get("date_override") == "2024-01-15"

    def test_gather_dest_dir_flag(self, tmp_path):
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text("specification:\n  version: '2.0'\nregistry:\n  devices: []\n  releases: []\n")
        dest = str(tmp_path / "dest")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "gather", "my-preset", "--dest-dir", dest]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.gather_bsp.return_value = GatherResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.gather_bsp.call_args
                assert kwargs.get("dest_dir") == dest

    def test_gather_by_components(self, tmp_path):
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text("specification:\n  version: '2.0'\nregistry:\n  devices: []\n  releases: []\n")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "gather", "--device", "rpi5", "--release", "scarthgap"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.gather_by_components.return_value = GatherResult()
                from bsp.cli import main as cli_main
                cli_main()
                mock_mgr.gather_by_components.assert_called_once()


# =============================================================================
# BspManager gather methods (mocked backend)
# =============================================================================


GATHER_REGISTRY_YAML = """
specification:
  version: "2.0"
containers:
  ubuntu-22.04:
    image: "test/ubuntu-22.04:latest"
registry:
  devices:
    - slug: rpi5
      description: "Raspberry Pi 5"
      vendor: raspberrypi
      soc_vendor: broadcom
      includes: []
  releases:
    - slug: scarthgap
      description: "Scarthgap"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: rpi5-scarthgap
      description: "RPI5 Scarthgap BSP"
      device: rpi5
      release: scarthgap
      features: []
      build:
        container: ubuntu-22.04
        path: build/rpi5/scarthgap
deploy:
  provider: azure
  container: bsp-artifacts
  account_url: https://myaccount.blob.core.windows.net
  prefix: "{vendor}/{device}/{release}/{date}"
"""


@pytest.fixture()
def gather_registry_file(tmp_path):
    reg = tmp_path / "bsp-registry.yaml"
    reg.write_text(GATHER_REGISTRY_YAML)
    return reg


class TestBspManagerGather:
    def test_gather_bsp_calls_gatherer(self, gather_registry_file, tmp_path):
        from bsp import BspManager
        mgr = BspManager(config_path=str(gather_registry_file))
        mgr.initialize()

        mock_result = GatherResult(dest_dir=tmp_path)
        with patch("bsp.bsp_manager.ArtifactGatherer") as MockGatherer:
            mock_instance = MockGatherer.return_value
            mock_instance.gather.return_value = mock_result
            with patch("bsp.bsp_manager.create_backend") as mock_factory:
                mock_backend = MagicMock()
                mock_factory.return_value = mock_backend
                result = mgr.gather_bsp(
                    "rpi5-scarthgap",
                    dest_dir=str(tmp_path),
                    dry_run=True,
                )

        assert isinstance(result, GatherResult)

    def test_gather_by_components_calls_gatherer(self, gather_registry_file, tmp_path):
        from bsp import BspManager
        mgr = BspManager(config_path=str(gather_registry_file))
        mgr.initialize()

        mock_result = GatherResult(dest_dir=tmp_path)
        with patch("bsp.bsp_manager.ArtifactGatherer") as MockGatherer:
            mock_instance = MockGatherer.return_value
            mock_instance.gather.return_value = mock_result
            with patch("bsp.bsp_manager.create_backend") as mock_factory:
                mock_backend = MagicMock()
                mock_factory.return_value = mock_backend
                result = mgr.gather_by_components(
                    "rpi5", "scarthgap",
                    dest_dir=str(tmp_path),
                    dry_run=True,
                )

        assert isinstance(result, GatherResult)

    def test_gather_bsp_dry_run_no_container_required(self, gather_registry_file, tmp_path):
        """Dry run should not exit even when container is not configured."""
        from bsp import BspManager
        from bsp.models import DeployConfig
        from dataclasses import replace

        mgr = BspManager(config_path=str(gather_registry_file))
        mgr.initialize()
        # Override model.deploy to omit container
        mgr.model = replace(mgr.model, deploy=DeployConfig(provider="azure", container=None))

        mock_result = GatherResult(dest_dir=tmp_path)
        with patch("bsp.bsp_manager.ArtifactGatherer") as MockGatherer:
            mock_instance = MockGatherer.return_value
            mock_instance.gather.return_value = mock_result
            with patch("bsp.bsp_manager.create_backend") as mock_factory:
                mock_factory.return_value = MagicMock()
                # dry_run=True so missing container should not cause sys.exit
                result = mgr.gather_bsp("rpi5-scarthgap", dry_run=True)

        assert isinstance(result, GatherResult)
