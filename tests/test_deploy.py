"""
Tests for cloud storage artifact deployment.

Covers:
- DeployConfig model (defaults, YAML parsing, RegistryRoot integration)
- ArtifactDeployer.collect_artifacts and compose_remote_prefix (no cloud needed)
- ArtifactDeployer.generate_manifest
- AzureStorageBackend (mocked)
- AwsStorageBackend (mocked)
- create_backend factory
- CLI deploy command argument parsing
- BspManager deploy methods (mocked)
"""

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from bsp.models import (
    DeployConfig,
    RegistryRoot,
    Registry,
    Specification,
    BspPreset,
)
from bsp.deployer import ArtifactDeployer, DeployResult, UploadedArtifact
from bsp.storage import create_backend
from bsp.storage.base import CloudStorageBackend


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def default_deploy_config():
    return DeployConfig()


@pytest.fixture()
def azure_deploy_config():
    return DeployConfig(
        provider="azure",
        container="bsp-artifacts",
        account_url="https://myaccount.blob.core.windows.net",
        prefix="{vendor}/{device}/{release}/{date}",
    )


@pytest.fixture()
def aws_deploy_config():
    return DeployConfig(
        provider="aws",
        bucket="bsp-s3-artifacts",
        region="eu-west-1",
    )


class _FakeBackend(CloudStorageBackend):
    """Minimal in-memory backend for testing ArtifactDeployer logic."""

    def __init__(self, dry_run=False):
        super().__init__(dry_run=dry_run)
        self.uploaded: dict = {}  # remote_path → local_path

    def upload_file(self, local_path, remote_path):
        if self.dry_run:
            return f"dry-run:{remote_path}"
        self.uploaded[remote_path] = local_path
        return f"fake://{remote_path}"

    def download_file(self, remote_path, local_path):
        pass  # not exercised by deploy tests

    def list_artifacts(self, remote_prefix):
        return [k for k in self.uploaded if k.startswith(remote_prefix)]

    def get_upload_url(self, remote_path):
        return f"fake://{remote_path}"


# =============================================================================
# DeployConfig model tests
# =============================================================================


class TestDeployConfigDefaults:
    def test_default_provider(self, default_deploy_config):
        assert default_deploy_config.provider == "azure"

    def test_default_container_is_none(self, default_deploy_config):
        assert default_deploy_config.container is None

    def test_default_bucket_is_none(self, default_deploy_config):
        assert default_deploy_config.bucket is None

    def test_default_account_url_is_none(self, default_deploy_config):
        assert default_deploy_config.account_url is None

    def test_default_prefix_is_none(self, default_deploy_config):
        assert default_deploy_config.prefix is None

    def test_default_patterns_non_empty(self, default_deploy_config):
        assert len(default_deploy_config.patterns) > 0
        assert any("wic" in p for p in default_deploy_config.patterns)

    def test_default_artifact_dirs(self, default_deploy_config):
        assert "tmp/deploy/images" in default_deploy_config.artifact_dirs

    def test_default_include_manifest_true(self, default_deploy_config):
        assert default_deploy_config.include_manifest is True

    def test_default_region_is_none(self, default_deploy_config):
        assert default_deploy_config.region is None

    def test_default_profile_is_none(self, default_deploy_config):
        assert default_deploy_config.profile is None


class TestDeployConfigCustom:
    def test_azure_config(self, azure_deploy_config):
        assert azure_deploy_config.provider == "azure"
        assert azure_deploy_config.container == "bsp-artifacts"
        assert azure_deploy_config.account_url == "https://myaccount.blob.core.windows.net"

    def test_aws_config(self, aws_deploy_config):
        assert aws_deploy_config.provider == "aws"
        assert aws_deploy_config.bucket == "bsp-s3-artifacts"
        assert aws_deploy_config.region == "eu-west-1"

    def test_custom_patterns(self):
        cfg = DeployConfig(patterns=["**/*.wic.gz", "**/*.tar.bz2"])
        assert cfg.patterns == ["**/*.wic.gz", "**/*.tar.bz2"]

    def test_custom_artifact_dirs(self):
        cfg = DeployConfig(artifact_dirs=["tmp/deploy/images"])
        assert cfg.artifact_dirs == ["tmp/deploy/images"]

    def test_no_manifest(self):
        cfg = DeployConfig(include_manifest=False)
        assert cfg.include_manifest is False


class TestDeployConfigInRegistryRoot:
    def test_registry_root_deploy_defaults_to_none(self):
        root = RegistryRoot(specification=Specification(version="2.1"), registry=Registry())
        assert root.deploy is None

    def test_registry_root_with_deploy_config(self):
        deploy = DeployConfig(provider="azure", container="my-container")
        root = RegistryRoot(
            specification=Specification(version="2.1"),
            registry=Registry(),
            deploy=deploy,
        )
        assert root.deploy is not None
        assert root.deploy.provider == "azure"
        assert root.deploy.container == "my-container"

    def test_bsp_preset_deploy_defaults_to_none(self):
        preset = BspPreset(
            name="my-preset",
            description="test",
            device="dev",
            release="rel",
        )
        assert preset.deploy is None

    def test_bsp_preset_with_deploy(self):
        deploy = DeployConfig(provider="aws", bucket="my-bucket")
        preset = BspPreset(
            name="my-preset",
            description="test",
            device="dev",
            release="rel",
            deploy=deploy,
        )
        assert preset.deploy is not None
        assert preset.deploy.provider == "aws"


class TestDeployConfigYamlParsing:
    """Verify that a registry YAML with a deploy: block round-trips correctly."""

    def test_registry_with_deploy_block(self, tmp_path):
        from bsp.utils import get_registry_from_yaml_file

        yaml_content = """
specification:
  version: "2.1"
registry:
  devices:
    - slug: my-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: test-soc
  releases:
    - slug: scarthgap
      description: "Scarthgap"
deploy:
  provider: azure
  container: bsp-artifacts
  account_url: https://myaccount.blob.core.windows.net
  prefix: "{vendor}/{device}/{release}/{date}"
  patterns:
    - "**/*.wic.gz"
    - "**/*.tar.bz2"
  artifact_dirs:
    - tmp/deploy/images
  include_manifest: true
"""
        registry_file = tmp_path / "bsp-registry.yaml"
        registry_file.write_text(yaml_content)

        root = get_registry_from_yaml_file(registry_file)
        assert root.deploy is not None
        assert root.deploy.provider == "azure"
        assert root.deploy.container == "bsp-artifacts"
        assert root.deploy.account_url == "https://myaccount.blob.core.windows.net"
        assert root.deploy.prefix == "{vendor}/{device}/{release}/{date}"
        assert "**/*.wic.gz" in root.deploy.patterns
        assert "tmp/deploy/images" in root.deploy.artifact_dirs
        assert root.deploy.include_manifest is True

    def test_registry_without_deploy_block(self, tmp_path):
        from bsp.utils import get_registry_from_yaml_file

        yaml_content = """
specification:
  version: "2.1"
registry:
  devices: []
  releases: []
"""
        registry_file = tmp_path / "bsp-registry.yaml"
        registry_file.write_text(yaml_content)
        root = get_registry_from_yaml_file(registry_file)
        assert root.deploy is None


# =============================================================================
# ArtifactDeployer tests
# =============================================================================


class TestCollectArtifacts:
    def test_collects_matching_files(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "my-image.wic.gz").write_text("data")
        (deploy_dir / "my-image.tar.bz2").write_text("data")
        (deploy_dir / "some-other.txt").write_text("data")

        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz", "**/*.tar.bz2"],
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        artifacts = deployer.collect_artifacts(str(tmp_path))

        names = {a.name for a in artifacts}
        assert "my-image.wic.gz" in names
        assert "my-image.tar.bz2" in names
        assert "some-other.txt" not in names

    def test_returns_empty_when_dir_missing(self, tmp_path):
        cfg = DeployConfig(artifact_dirs=["nonexistent/dir"])
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        assert deployer.collect_artifacts(str(tmp_path)) == []

    def test_deduplicates_results(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic").write_text("data")

        # Pattern matches the same file twice via two different patterns
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic", "*.wic"],
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        artifacts = deployer.collect_artifacts(str(tmp_path))
        assert len([a for a in artifacts if a.name == "image.wic"]) == 1

    def test_no_artifacts_returns_empty_list(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        assert deployer.collect_artifacts(str(tmp_path)) == []


class TestComposeRemotePrefix:
    def test_default_template(self):
        cfg = DeployConfig()
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        prefix = deployer.compose_remote_prefix(
            device="my-board", release="scarthgap", vendor="acme"
        )
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert "my-board" in prefix
        assert "scarthgap" in prefix
        assert "acme" in prefix
        assert today in prefix

    def test_custom_template(self):
        cfg = DeployConfig(prefix="{vendor}/{device}/{release}")
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        prefix = deployer.compose_remote_prefix(
            device="rpi4", release="kirkstone", vendor="rpi"
        )
        assert prefix == "rpi/rpi4/kirkstone"

    def test_date_placeholder(self):
        cfg = DeployConfig(prefix="builds/{date}")
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        prefix = deployer.compose_remote_prefix()
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert prefix == f"builds/{today}"

    def test_unknown_placeholders_preserved(self):
        """Unknown placeholders use 'unknown' as fallback, not raise."""
        cfg = DeployConfig(prefix="{vendor}/{device}/{release}")
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        prefix = deployer.compose_remote_prefix()
        assert "unknown" in prefix

    def test_no_leading_trailing_slashes(self):
        cfg = DeployConfig(prefix="/builds/{device}/")
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        prefix = deployer.compose_remote_prefix(device="board")
        assert not prefix.startswith("/")
        assert not prefix.endswith("/")


class TestGenerateManifest:
    def test_manifest_structure(self, tmp_path):
        artifact_path = tmp_path / "image.wic.gz"
        artifact_path.write_bytes(b"fake data")

        result = DeployResult(
            artifacts=[
                UploadedArtifact(
                    local_path=artifact_path,
                    remote_url="fake://prefix/image.wic.gz",
                    size_bytes=9,
                    sha256="abc123",
                )
            ]
        )
        cfg = DeployConfig(provider="azure")
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        manifest_json = deployer.generate_manifest(
            result, device="board", release="scarthgap", distro="poky", vendor="acme"
        )
        data = json.loads(manifest_json)

        assert data["schema_version"] == "1"
        assert data["provider"] == "azure"
        assert data["build"]["device"] == "board"
        assert data["build"]["release"] == "scarthgap"
        assert data["build"]["distro"] == "poky"
        assert data["build"]["vendor"] == "acme"
        assert len(data["artifacts"]) == 1
        assert data["artifacts"][0]["name"] == "image.wic.gz"
        assert data["artifacts"][0]["sha256"] == "abc123"
        assert "generated_at" in data

    def test_manifest_total_size(self, tmp_path):
        result = DeployResult(
            artifacts=[
                UploadedArtifact(tmp_path / "a.wic", "url1", 100, "sha1"),
                UploadedArtifact(tmp_path / "b.wic", "url2", 200, "sha2"),
            ]
        )
        cfg = DeployConfig()
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        data = json.loads(deployer.generate_manifest(result))
        assert data["total_size_bytes"] == 300


class TestDeployRun:
    def test_full_deploy_run(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        f1 = deploy_dir / "core-image.wic.gz"
        f1.write_bytes(b"wic content")

        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=True,
            prefix="acme/board/scarthgap/{date}",
        )
        backend = _FakeBackend()
        deployer = ArtifactDeployer(cfg, backend)
        result = deployer.deploy(
            str(tmp_path), device="board", release="scarthgap", vendor="acme"
        )

        assert result.success_count == 1
        assert result.artifacts[0].local_path == f1
        assert "core-image.wic.gz" in result.artifacts[0].remote_url
        assert result.manifest_url is not None

    def test_dry_run_no_uploads(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"data")

        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
        )
        backend = _FakeBackend(dry_run=True)
        deployer = ArtifactDeployer(cfg, backend)
        result = deployer.deploy(str(tmp_path))

        assert result.dry_run is True
        assert len(backend.uploaded) == 0
        assert result.success_count == 1  # recorded even in dry-run
        assert result.artifacts[0].remote_url.startswith("dry-run:")

    def test_partial_failure_continues(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "good.wic.gz").write_bytes(b"ok")
        (deploy_dir / "bad.wic.gz").write_bytes(b"fail")

        upload_calls = []

        class _FailOnBad(CloudStorageBackend):
            def upload_file(self, local_path, remote_path):
                if "bad" in local_path.name:
                    raise RuntimeError("upload failed")
                upload_calls.append(remote_path)
                return f"ok://{remote_path}"

            def download_file(self, remote_path, local_path):
                pass  # not exercised here

            def list_artifacts(self, remote_prefix):
                return []

        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
        )
        deployer = ArtifactDeployer(cfg, _FailOnBad())
        result = deployer.deploy(str(tmp_path))

        assert result.success_count == 1
        assert len(upload_calls) == 1


# =============================================================================
# ArtifactDeployer progress output tests
# =============================================================================


class TestDeployProgressOutput:
    """Verify that deploy() emits progress messages to stdout without --verbose."""

    def _make_deploy_dir(self, tmp_path, filenames=("core-image.wic.gz",)):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        for name in filenames:
            (deploy_dir / name).write_bytes(b"data")

    def test_deploy_banner_printed(self, tmp_path, capsys):
        self._make_deploy_dir(tmp_path)
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            prefix="acme/board/scarthgap",
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        deployer.deploy(str(tmp_path), device="board", vendor="acme")
        out = capsys.readouterr().out
        assert "Deploying" in out
        assert "1 artifact(s)" in out

    def test_per_file_upload_printed(self, tmp_path, capsys):
        self._make_deploy_dir(tmp_path)
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            prefix="acme/board/scarthgap",
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        deployer.deploy(str(tmp_path))
        out = capsys.readouterr().out
        assert "Uploading core-image.wic.gz" in out

    def test_no_artifacts_message_printed(self, tmp_path, capsys):
        # Empty dir — no matching files
        (tmp_path / "tmp" / "deploy" / "images").mkdir(parents=True)
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        deployer.deploy(str(tmp_path))
        out = capsys.readouterr().out
        assert "No artifacts found" in out

    def test_dry_run_banner_printed(self, tmp_path, capsys):
        self._make_deploy_dir(tmp_path)
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            prefix="acme/board/scarthgap",
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend(dry_run=True))
        deployer.deploy(str(tmp_path))
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "1 artifact(s)" in out

    def test_multiple_files_each_printed(self, tmp_path, capsys):
        self._make_deploy_dir(tmp_path, filenames=("a.wic.gz", "b.wic.gz"))
        cfg = DeployConfig(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            prefix="acme/board/scarthgap",
        )
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        deployer.deploy(str(tmp_path))
        out = capsys.readouterr().out
        assert "Uploading a.wic.gz" in out
        assert "Uploading b.wic.gz" in out


# =============================================================================
# Storage backend tests (mocked SDK)
# =============================================================================


class TestAzureStorageBackend:
    def test_dry_run_no_sdk_required(self):
        """dry_run=True must work without azure SDK installed."""
        from bsp.storage.azure import AzureStorageBackend
        backend = AzureStorageBackend(
            container_name="test", dry_run=True
        )
        url = backend.upload_file(Path("/tmp/fake.wic"), "prefix/fake.wic")
        assert url.startswith("dry-run:")

    def test_dry_run_list_returns_empty(self):
        from bsp.storage.azure import AzureStorageBackend
        backend = AzureStorageBackend(container_name="test", dry_run=True)
        assert backend.list_artifacts("some/prefix") == []

    def test_upload_file_calls_sdk(self, tmp_path):
        """Upload delegates to BlobServiceClient when SDK is available."""
        artifact = tmp_path / "image.wic.gz"
        artifact.write_bytes(b"data")

        mock_container_client = MagicMock()
        mock_blob_service = MagicMock()
        mock_blob_service.url = "https://myaccount.blob.core.windows.net"
        mock_blob_service.get_container_client.return_value = mock_container_client

        from bsp.storage import azure as azure_mod
        with patch.object(azure_mod, "_INSTALL_HINT", ""):
            try:
                import azure.storage.blob  # noqa: F401
                HAS_SDK = True
            except ImportError:
                HAS_SDK = False

        if not HAS_SDK:
            pytest.skip("azure-storage-blob not installed")

        from bsp.storage.azure import AzureStorageBackend
        with patch("azure.storage.blob.BlobServiceClient") as mock_cls:
            mock_cls.from_connection_string.return_value = mock_blob_service
            backend = AzureStorageBackend(
                container_name="test-container",
                connection_string="DefaultEndpointsProtocol=https;...",
            )
            backend.upload_file(artifact, "prefix/image.wic.gz")

        mock_container_client.upload_blob.assert_called_once()

    def test_raises_import_error_without_sdk(self):
        """ImportError raised when azure SDK missing and dry_run=False."""
        import importlib
        import sys

        # Temporarily hide the azure package from imports
        azure_modules = [k for k in sys.modules if k.startswith("azure")]
        saved = {k: sys.modules.pop(k) for k in azure_modules}

        try:
            with patch.dict("sys.modules", {"azure.storage.blob": None}):
                from bsp.storage import azure as azure_mod
                importlib.reload(azure_mod)
                with pytest.raises(ImportError, match="azure-storage-blob"):
                    azure_mod.AzureStorageBackend(
                        container_name="c", connection_string="x"
                    )
        finally:
            sys.modules.update(saved)

    def test_raises_value_error_without_credentials(self):
        """ValueError raised when no URL or connection string configured."""
        try:
            import azure.storage.blob  # noqa: F401
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

        from bsp.storage.azure import AzureStorageBackend
        env = {k: v for k, v in os.environ.items()
               if k not in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT_URL")}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises((ValueError, ImportError)):
                AzureStorageBackend(container_name="c")


class TestAwsStorageBackend:
    def test_dry_run_no_sdk_required(self):
        from bsp.storage.aws import AwsStorageBackend
        backend = AwsStorageBackend(bucket_name="my-bucket", dry_run=True)
        url = backend.upload_file(Path("/tmp/fake.wic"), "prefix/fake.wic")
        assert url.startswith("dry-run:")

    def test_dry_run_list_returns_empty(self):
        from bsp.storage.aws import AwsStorageBackend
        backend = AwsStorageBackend(bucket_name="my-bucket", dry_run=True)
        assert backend.list_artifacts("some/prefix") == []

    def test_upload_file_calls_sdk(self, tmp_path):
        artifact = tmp_path / "image.wic.gz"
        artifact.write_bytes(b"data")

        try:
            import boto3  # noqa: F401
            HAS_SDK = True
        except ImportError:
            HAS_SDK = False

        if not HAS_SDK:
            pytest.skip("boto3 not installed")

        from bsp.storage.aws import AwsStorageBackend
        mock_s3 = MagicMock()
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.client.return_value = mock_s3
            mock_session_cls.return_value = mock_session

            backend = AwsStorageBackend(bucket_name="test-bucket")
            backend.upload_file(artifact, "prefix/image.wic.gz")

        mock_s3.upload_file.assert_called_once_with(
            str(artifact), "test-bucket", "prefix/image.wic.gz"
        )

    def test_raises_import_error_without_sdk(self):
        with patch.dict("sys.modules", {"boto3": None}):
            import importlib
            from bsp.storage import aws as aws_mod
            importlib.reload(aws_mod)
            with pytest.raises(ImportError, match="boto3"):
                aws_mod.AwsStorageBackend(bucket_name="b")

    def test_get_upload_url(self):
        from bsp.storage.aws import AwsStorageBackend
        backend = AwsStorageBackend(bucket_name="my-bucket", dry_run=True)
        assert backend.get_upload_url("a/b/c.wic") == "s3://my-bucket/a/b/c.wic"


class TestCreateBackendFactory:
    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown storage provider"):
            create_backend("gcp")

    def test_azure_provider_returns_azure_backend(self):
        from bsp.storage.azure import AzureStorageBackend
        backend = create_backend("azure", container_name="c", dry_run=True)
        assert isinstance(backend, AzureStorageBackend)

    def test_aws_provider_returns_aws_backend(self):
        from bsp.storage.aws import AwsStorageBackend
        backend = create_backend("aws", bucket_name="b", dry_run=True)
        assert isinstance(backend, AwsStorageBackend)

    def test_provider_case_insensitive(self):
        from bsp.storage.azure import AzureStorageBackend
        backend = create_backend("Azure", container_name="c", dry_run=True)
        assert isinstance(backend, AzureStorageBackend)


# =============================================================================
# CLI argument parsing tests
# =============================================================================


class TestDeployCliArguments:
    """Verify the deploy subcommand argument structure."""

    def _parse(self, argv):
        """Parse CLI args and return the Namespace, bypassing sys.exit."""
        import argparse
        from bsp.cli import main
        # Capture the parser by inspecting what argparse would produce
        # We use a subprocess-free approach: monkeypatch sys.argv
        old_argv = sys.argv[:]
        sys.argv = ["bsp"] + argv
        try:
            # We just want to test the parser, not execute commands
            from bsp.cli import _collect_deploy_overrides
            import argparse
            # Build a minimal parser that mirrors the deploy subparser
            p = argparse.ArgumentParser()
            p.add_argument("bsp_name", nargs="?")
            p.add_argument("--device", "-d")
            p.add_argument("--release")
            p.add_argument("--provider", dest="deploy_provider")
            p.add_argument("--container", "--bucket", dest="deploy_container")
            p.add_argument("--prefix", dest="deploy_prefix")
            p.add_argument("--pattern", action="append", dest="deploy_patterns")
            p.add_argument("--dry-run", action="store_true", dest="dry_run")
            p.add_argument(
                "--no-build-manifest",
                action="store_false",
                default=None,
                dest="include_build_manifest",
            )
            return p.parse_args(argv)
        finally:
            sys.argv = old_argv

    def test_deploy_with_preset(self):
        args = self._parse(["my-preset"])
        assert args.bsp_name == "my-preset"

    def test_deploy_with_device_release(self):
        args = self._parse(["--device", "qemu-arm64", "--release", "scarthgap"])
        assert args.device == "qemu-arm64"
        assert args.release == "scarthgap"

    def test_deploy_provider_flag(self):
        args = self._parse(["my-preset", "--provider", "aws"])
        assert args.deploy_provider == "aws"

    def test_deploy_container_flag(self):
        args = self._parse(["my-preset", "--container", "my-bucket"])
        assert args.deploy_container == "my-bucket"

    def test_deploy_dry_run_flag(self):
        args = self._parse(["my-preset", "--dry-run"])
        assert args.dry_run is True

    def test_collect_deploy_overrides_empty(self):
        from bsp.cli import _collect_deploy_overrides
        args = self._parse(["preset"])
        overrides = _collect_deploy_overrides(args)
        assert overrides == {}

    def test_no_build_manifest_flag(self):
        from bsp.cli import _collect_deploy_overrides
        args = self._parse(["preset", "--no-build-manifest"])
        assert _collect_deploy_overrides(args)["include_build_manifest"] is False

    def test_collect_deploy_overrides_with_values(self):
        from bsp.cli import _collect_deploy_overrides
        args = self._parse([
            "preset",
            "--provider", "aws",
            "--container", "my-bucket",
            "--prefix", "builds/{device}",
            "--pattern", "**/*.wic.gz",
        ])
        overrides = _collect_deploy_overrides(args)
        assert overrides["provider"] == "aws"
        assert overrides["container"] == "my-bucket"
        assert overrides["prefix"] == "builds/{device}"
        assert overrides["patterns"] == ["**/*.wic.gz"]


# =============================================================================
# BspManager deploy integration (mocked resolver/storage)
# =============================================================================


class TestBspManagerDeploy:
    def _make_manager(self, tmp_path, deploy_cfg=None):
        """Create a minimal BspManager backed by a temp registry file."""
        from bsp.utils import get_registry_from_yaml_file
        from bsp.bsp_manager import BspManager

        deploy_block = ""
        if deploy_cfg:
            deploy_block = f"""
deploy:
  provider: {deploy_cfg.get("provider", "azure")}
  container: {deploy_cfg.get("container", "bsp-artifacts")}
"""

        yaml_content = f"""
specification:
  version: "2.1"
registry:
  devices:
    - slug: my-device
      description: "Test Device"
      vendor: acme
      soc_vendor: arm
      includes:
        - kas/device.yaml
  releases:
    - slug: scarthgap
      description: "Scarthgap"
      includes:
        - kas/release.yaml
  features: []
  bsp:
    - name: my-preset
      description: "My Preset"
      device: my-device
      release: scarthgap
{deploy_block}
"""
        registry_file = tmp_path / "bsp-registry.yaml"
        registry_file.write_text(yaml_content)

        mgr = BspManager(str(registry_file))
        mgr.load_configuration()
        from bsp.resolver import V2Resolver
        mgr.resolver = V2Resolver(mgr.model, mgr.containers)
        return mgr

    def test_deploy_bsp_dry_run_no_artifacts(self, tmp_path):
        mgr = self._make_manager(tmp_path, deploy_cfg={"provider": "azure", "container": "c"})
        # Patch deployer to avoid actual cloud calls
        with patch("bsp.bsp_manager.ArtifactDeployer") as MockDeployer:
            mock_instance = MagicMock()
            mock_result = DeployResult(dry_run=True)
            mock_instance.deploy.return_value = mock_result
            MockDeployer.return_value = mock_instance

            with patch("bsp.bsp_manager.create_backend") as mock_factory:
                mock_factory.return_value = _FakeBackend(dry_run=True)
                result = mgr.deploy_bsp("my-preset", dry_run=True)

        assert result is not None

    def test_deploy_by_components_dry_run(self, tmp_path):
        mgr = self._make_manager(tmp_path, deploy_cfg={"provider": "aws", "container": "my-bucket"})
        with patch("bsp.bsp_manager.ArtifactDeployer") as MockDeployer:
            mock_instance = MagicMock()
            mock_result = DeployResult(dry_run=True)
            mock_instance.deploy.return_value = mock_result
            MockDeployer.return_value = mock_instance

            with patch("bsp.bsp_manager.create_backend") as mock_factory:
                mock_factory.return_value = _FakeBackend(dry_run=True)
                result = mgr.deploy_by_components(
                    "my-device", "scarthgap", dry_run=True
                )

        assert result is not None

    def test_resolve_deploy_config_uses_global(self, tmp_path):
        from bsp.bsp_manager import BspManager
        mgr = self._make_manager(tmp_path, deploy_cfg={"provider": "azure", "container": "global-c"})
        resolved = mgr.resolver.resolve("my-device", "scarthgap")
        cfg = mgr._resolve_deploy_config(resolved)
        assert cfg.provider == "azure"
        assert cfg.container == "global-c"

    def test_resolve_deploy_config_cli_override(self, tmp_path):
        from bsp.bsp_manager import BspManager
        mgr = self._make_manager(tmp_path, deploy_cfg={"provider": "azure", "container": "global-c"})
        resolved = mgr.resolver.resolve("my-device", "scarthgap")
        cfg = mgr._resolve_deploy_config(resolved, deploy_overrides={"container": "cli-c"})
        assert cfg.container == "cli-c"
        assert cfg.provider == "azure"  # not overridden

    def _make_manager_with_preset_deploy(self, tmp_path, global_cfg=None, preset_cfg=None):
        """Create a BspManager with optional global and per-preset deploy configs."""
        from bsp.bsp_manager import BspManager

        global_deploy_block = ""
        if global_cfg:
            lines = [f"  {k}: {v}" for k, v in global_cfg.items()]
            global_deploy_block = "deploy:\n" + "\n".join(lines) + "\n"

        preset_deploy_block = ""
        if preset_cfg:
            lines = [f"        {k}: {v}" for k, v in preset_cfg.items()]
            preset_deploy_block = "      deploy:\n" + "\n".join(lines) + "\n"

        yaml_content = f"""
specification:
  version: "2.1"

{global_deploy_block}
registry:
  devices:
    - slug: my-device
      description: "Test Device"
      vendor: acme
      soc_vendor: arm
      includes: []
  releases:
    - slug: scarthgap
      description: "Scarthgap"
      includes: []
  features: []
  bsp:
    - name: my-preset
      description: "My Preset"
      device: my-device
      release: scarthgap
{preset_deploy_block}
"""
        registry_file = tmp_path / "bsp-registry.yaml"
        registry_file.write_text(yaml_content)

        mgr = BspManager(str(registry_file))
        mgr.load_configuration()
        from bsp.resolver import V2Resolver
        mgr.resolver = V2Resolver(mgr.model, mgr.containers)
        return mgr

    def test_resolve_deploy_config_preset_overrides_global(self, tmp_path):
        """Preset-level deploy config should override the global deploy config."""
        mgr = self._make_manager_with_preset_deploy(
            tmp_path,
            global_cfg={"provider": "azure", "container": "global-c"},
            preset_cfg={"container": "preset-c"},
        )
        resolved, preset = mgr.resolver.resolve_preset("my-preset")
        cfg = mgr._resolve_deploy_config(resolved, preset=preset)
        assert cfg.container == "preset-c"   # overridden by preset
        assert cfg.provider == "azure"        # kept from global

    def test_resolve_deploy_config_preset_overrides_provider(self, tmp_path):
        """Preset-level deploy config can switch provider from azure to aws."""
        mgr = self._make_manager_with_preset_deploy(
            tmp_path,
            global_cfg={"provider": "azure", "container": "global-c"},
            preset_cfg={"provider": "aws", "container": "preset-bucket"},
        )
        resolved, preset = mgr.resolver.resolve_preset("my-preset")
        cfg = mgr._resolve_deploy_config(resolved, preset=preset)
        assert cfg.provider == "aws"
        assert cfg.container == "preset-bucket"

    def test_resolve_deploy_config_cli_overrides_preset_and_global(self, tmp_path):
        """CLI overrides should take precedence over both preset and global config."""
        mgr = self._make_manager_with_preset_deploy(
            tmp_path,
            global_cfg={"provider": "azure", "container": "global-c"},
            preset_cfg={"container": "preset-c"},
        )
        resolved, preset = mgr.resolver.resolve_preset("my-preset")
        cfg = mgr._resolve_deploy_config(
            resolved, preset=preset, deploy_overrides={"container": "cli-c"}
        )
        assert cfg.container == "cli-c"

    def test_resolve_deploy_config_no_preset_deploy(self, tmp_path):
        """When the preset has no deploy block, the global config is used unchanged."""
        mgr = self._make_manager_with_preset_deploy(
            tmp_path,
            global_cfg={"provider": "azure", "container": "global-c"},
            preset_cfg=None,
        )
        resolved, preset = mgr.resolver.resolve_preset("my-preset")
        cfg = mgr._resolve_deploy_config(resolved, preset=preset)
        assert cfg.container == "global-c"
        assert cfg.provider == "azure"

    def test_resolve_deploy_config_preset_only_no_global(self, tmp_path):
        """When there's no global deploy config, preset-level deploy config is used."""
        mgr = self._make_manager_with_preset_deploy(
            tmp_path,
            global_cfg=None,
            preset_cfg={"provider": "aws", "container": "preset-bucket"},
        )
        resolved, preset = mgr.resolver.resolve_preset("my-preset")
        cfg = mgr._resolve_deploy_config(resolved, preset=preset)
        assert cfg.provider == "aws"
        assert cfg.container == "preset-bucket"


# =============================================================================
# CloudStorageBackend.upload_directory tests
# =============================================================================


class TestUploadDirectory:
    def test_upload_directory_uploads_matching_files(self, tmp_path):
        (tmp_path / "a.wic.gz").write_bytes(b"a")
        (tmp_path / "b.ext4").write_bytes(b"b")
        (tmp_path / "skip.txt").write_bytes(b"s")

        backend = _FakeBackend()
        backend.upload_directory(tmp_path, "prefix", "*.wic.gz")

        assert any("a.wic.gz" in k for k in backend.uploaded)
        assert not any("skip.txt" in k for k in backend.uploaded)

    def test_upload_directory_missing_dir(self, tmp_path, caplog):
        backend = _FakeBackend()
        result = backend.upload_directory(tmp_path / "does-not-exist", "prefix")
        assert result == []

    def test_upload_directory_returns_urls(self, tmp_path):
        (tmp_path / "image.wic.gz").write_bytes(b"data")
        backend = _FakeBackend()
        urls = backend.upload_directory(tmp_path, "my/prefix", "*.wic.gz")
        assert len(urls) == 1
        assert "image.wic.gz" in urls[0]


# =============================================================================
# YoctoCacheConfig model tests
# =============================================================================


class TestYoctoCacheConfig:
    def test_defaults_disabled(self):
        from bsp.models import YoctoCacheConfig
        cfg = YoctoCacheConfig()
        assert cfg.enabled is False

    def test_defaults_include_both(self):
        from bsp.models import YoctoCacheConfig
        cfg = YoctoCacheConfig(enabled=True)
        assert cfg.downloads is True
        assert cfg.sstate is True

    def test_custom_paths(self):
        from bsp.models import YoctoCacheConfig
        cfg = YoctoCacheConfig(
            enabled=True,
            downloads_path="/mnt/dl",
            sstate_path="/mnt/ss",
        )
        assert cfg.downloads_path == "/mnt/dl"
        assert cfg.sstate_path == "/mnt/ss"

    def test_deploy_config_yocto_cache_defaults_none(self):
        cfg = DeployConfig()
        assert cfg.yocto_cache is None

    def test_deploy_config_yocto_cache_set(self):
        from bsp.models import YoctoCacheConfig
        cache_cfg = YoctoCacheConfig(enabled=True)
        cfg = DeployConfig(yocto_cache=cache_cfg)
        assert cfg.yocto_cache is not None
        assert cfg.yocto_cache.enabled is True

    def test_yocto_cache_yaml_parsing(self, tmp_path):
        from bsp.utils import get_registry_from_yaml_file

        yaml_content = """
specification:
  version: "2.0"
registry:
  devices: []
  releases: []
  features: []
  bsp: []
deploy:
  provider: azure
  container: my-artifacts
  yocto_cache:
    enabled: true
    downloads: true
    sstate: false
    downloads_path: /mnt/downloads
"""
        registry_file = tmp_path / "registry.yaml"
        registry_file.write_text(yaml_content)
        root = get_registry_from_yaml_file(registry_file)

        assert root.deploy is not None
        cache = root.deploy.yocto_cache
        assert cache is not None
        assert cache.enabled is True
        assert cache.downloads is True
        assert cache.sstate is False
        assert cache.downloads_path == "/mnt/downloads"


# =============================================================================
# ArtifactDeployer cache upload tests
# =============================================================================


class TestArtifactDeployerCacheUpload:
    """Test Yocto cache pack-and-upload logic."""

    def _make_deployer(self, enabled=True, downloads=True, sstate=True, backend=None):
        from bsp.models import YoctoCacheConfig
        cfg = DeployConfig(
            provider="azure",
            container="bsp-artifacts",
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            yocto_cache=YoctoCacheConfig(
                enabled=enabled,
                downloads=downloads,
                sstate=sstate,
            ),
        )
        if backend is None:
            backend = _FakeBackend()
        return ArtifactDeployer(cfg, backend)

    def test_cache_upload_uploads_downloads_dir(self, tmp_path):
        # Create a build dir with an artifact so deploy() doesn't early-exit
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        # Create a fake DL_DIR
        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()
        (dl_dir / "source.tar.gz").write_bytes(b"src")

        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)
        result = deployer.deploy(
            str(tmp_path), device="mydev", release="rel",
            downloads_path=str(dl_dir),
        )

        assert len(result.cache_uploads) == 1
        assert result.cache_uploads[0].cache_type == "downloads"
        # Check the archive was uploaded under cache/ sub-prefix
        uploaded_keys = list(backend.uploaded.keys())
        assert any("cache/downloads.tar.gz" in k for k in uploaded_keys)

    def test_cache_upload_uploads_sstate_dir(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        ss_dir = tmp_path / "sstate"
        ss_dir.mkdir()
        (ss_dir / "sig.siginfo").write_bytes(b"sig")

        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)
        result = deployer.deploy(
            str(tmp_path), device="mydev", release="rel",
            sstate_path=str(ss_dir),
        )

        assert len(result.cache_uploads) == 1
        assert result.cache_uploads[0].cache_type == "sstate"
        uploaded_keys = list(backend.uploaded.keys())
        assert any("cache/sstate.tar.gz" in k for k in uploaded_keys)

    def test_cache_upload_both_dirs(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()
        ss_dir = tmp_path / "sstate"
        ss_dir.mkdir()

        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)
        result = deployer.deploy(
            str(tmp_path), device="mydev", release="rel",
            downloads_path=str(dl_dir),
            sstate_path=str(ss_dir),
        )

        assert len(result.cache_uploads) == 2
        types = {uc.cache_type for uc in result.cache_uploads}
        assert types == {"downloads", "sstate"}

    def test_cache_upload_skipped_when_disabled(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()

        deployer = self._make_deployer(enabled=False)
        result = deployer.deploy(
            str(tmp_path), downloads_path=str(dl_dir),
        )

        assert result.cache_uploads == []

    def test_cache_upload_skipped_for_missing_dir(self, tmp_path, caplog):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)
        import logging
        with caplog.at_level(logging.INFO):
            result = deployer.deploy(
                str(tmp_path),
                downloads_path=str(tmp_path / "nonexistent"),
            )

        assert result.cache_uploads == []
        assert "not found" in caplog.text.lower() or "skipping" in caplog.text.lower()

    def test_cache_upload_dry_run(self, tmp_path):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()

        backend = _FakeBackend(dry_run=True)
        deployer = self._make_deployer(backend=backend)
        result = deployer.deploy(
            str(tmp_path), device="d", release="r",
            downloads_path=str(dl_dir),
        )

        assert len(result.cache_uploads) == 1
        assert result.cache_uploads[0].remote_url.startswith("dry-run:")
        # Nothing actually uploaded
        assert len(backend.uploaded) == 0

    def test_generate_manifest_includes_yocto_cache(self, tmp_path):
        from bsp.deployer import UploadedCache
        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)

        result = DeployResult(dry_run=False)
        result.artifacts = [
            UploadedArtifact(
                local_path=tmp_path / "img.wic.gz",
                remote_url="fake://pfx/img.wic.gz",
                size_bytes=100,
                sha256="abc",
            )
        ]
        result.cache_uploads = [
            UploadedCache(
                cache_type="downloads",
                local_archive=tmp_path / "downloads.tar.gz",
                remote_url="fake://pfx/cache/downloads.tar.gz",
                size_bytes=200,
                sha256="def",
            )
        ]

        manifest_json = deployer.generate_manifest(result, device="dev", release="rel")
        manifest = json.loads(manifest_json)

        assert "yocto_cache" in manifest
        assert "downloads" in manifest["yocto_cache"]
        assert manifest["yocto_cache"]["downloads"]["remote_url"] == "fake://pfx/cache/downloads.tar.gz"
        assert manifest["yocto_cache"]["downloads"]["sha256"] == "def"

    def test_generate_manifest_no_yocto_cache_when_empty(self):
        backend = _FakeBackend()
        deployer = self._make_deployer(backend=backend)
        result = DeployResult(dry_run=False)
        result.artifacts = [
            UploadedArtifact(
                local_path=Path("/tmp/img.wic"),
                remote_url="fake://pfx/img.wic",
                size_bytes=10,
                sha256="aa",
            )
        ]
        manifest = json.loads(deployer.generate_manifest(result))
        assert "yocto_cache" not in manifest

    def test_only_downloads_flag(self, tmp_path):
        """--no-deploy-cache-sstate skips the sstate upload."""
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"wic")

        dl_dir = tmp_path / "downloads"
        dl_dir.mkdir()
        ss_dir = tmp_path / "sstate"
        ss_dir.mkdir()

        backend = _FakeBackend()
        deployer = self._make_deployer(downloads=True, sstate=False, backend=backend)
        result = deployer.deploy(
            str(tmp_path), downloads_path=str(dl_dir), sstate_path=str(ss_dir),
        )

        assert len(result.cache_uploads) == 1
        assert result.cache_uploads[0].cache_type == "downloads"


# =============================================================================
# _collect_deploy_overrides cache extension tests
# =============================================================================


class TestCollectDeployOverridesCache:
    def _make_args(self, **kwargs):
        import argparse
        ns = argparse.Namespace()
        for k, v in kwargs.items():
            setattr(ns, k, v)
        return ns

    def test_no_deploy_cache_flag_no_override(self):
        from bsp.cli import _collect_deploy_overrides
        args = self._make_args(
            deploy_provider=None, deploy_container=None, deploy_prefix=None,
            deploy_patterns=None, deploy_archive_name=None, deploy_archive_format=None,
            deploy_cache=None,
        )
        overrides = _collect_deploy_overrides(args)
        assert "yocto_cache" not in overrides

    def test_deploy_cache_true_creates_yocto_cache(self):
        from bsp.cli import _collect_deploy_overrides
        from bsp.models import YoctoCacheConfig
        args = self._make_args(
            deploy_provider=None, deploy_container=None, deploy_prefix=None,
            deploy_patterns=None, deploy_archive_name=None, deploy_archive_format=None,
            deploy_cache=True, deploy_cache_downloads=True, deploy_cache_sstate=True,
        )
        overrides = _collect_deploy_overrides(args)
        assert "yocto_cache" in overrides
        assert isinstance(overrides["yocto_cache"], YoctoCacheConfig)
        assert overrides["yocto_cache"].enabled is True
        assert overrides["yocto_cache"].downloads is True
        assert overrides["yocto_cache"].sstate is True

    def test_deploy_cache_no_downloads(self):
        from bsp.cli import _collect_deploy_overrides
        args = self._make_args(
            deploy_provider=None, deploy_container=None, deploy_prefix=None,
            deploy_patterns=None, deploy_archive_name=None, deploy_archive_format=None,
            deploy_cache=True, deploy_cache_downloads=False, deploy_cache_sstate=True,
        )
        overrides = _collect_deploy_overrides(args)
        assert overrides["yocto_cache"].downloads is False
        assert overrides["yocto_cache"].sstate is True


# =============================================================================
# BspManager cache-path fallback tests
# =============================================================================


class TestBspManagerDeployCachePathResolution:
    @pytest.fixture()
    def deploy_registry_file(self, tmp_path):
        yaml_content = """
specification:
  version: "2.0"
registry:
  devices:
    - slug: dev
      description: "Device"
      vendor: acme
      soc_vendor: arm
      includes:
        - kas/dev.yaml
  releases:
    - slug: rel
      description: "Release"
      includes:
        - kas/rel.yaml
  features: []
  bsp:
    - name: dev-rel
      description: "Preset"
      device: dev
      release: rel
deploy:
  provider: azure
  container: bsp-artifacts
"""
        reg_file = tmp_path / "bsp-registry.yaml"
        reg_file.write_text(yaml_content)
        return reg_file

    def test_resolve_cache_paths_falls_back_to_yocto_defaults_when_unset(
        self, deploy_registry_file
    ):
        from bsp.bsp_manager import BspManager
        from bsp.models import YoctoCacheConfig

        mgr = BspManager(config_path=str(deploy_registry_file))
        mgr.initialize()
        mgr.env_manager = MagicMock()
        mgr.env_manager.get_value.return_value = None

        # Default artifact_dirs = ["tmp/deploy/images"] → TOPDIR = build_path
        cfg = DeployConfig(
            yocto_cache=YoctoCacheConfig(
                enabled=True,
                downloads=True,
                sstate=True,
            )
        )
        dl, ss = mgr._resolve_cache_paths(cfg, build_path="/tmp/yocto-build")

        assert dl == "/tmp/yocto-build/downloads"
        assert ss == "/tmp/yocto-build/sstate-cache"

    def test_resolve_cache_paths_nested_artifact_dirs(
        self, deploy_registry_file
    ):
        """artifact_dirs with build/ prefix → TOPDIR = build_path/build/."""
        from bsp.bsp_manager import BspManager
        from bsp.models import YoctoCacheConfig

        mgr = BspManager(config_path=str(deploy_registry_file))
        mgr.initialize()
        mgr.env_manager = MagicMock()
        mgr.env_manager.get_value.return_value = None

        cfg = DeployConfig(
            artifact_dirs=["build/tmp/deploy/images", "build/tmp/deploy/sdk"],
            yocto_cache=YoctoCacheConfig(
                enabled=True,
                downloads=True,
                sstate=True,
            )
        )
        dl, ss = mgr._resolve_cache_paths(cfg, build_path="/tmp/yocto-build")

        assert dl == "/tmp/yocto-build/build/downloads"
        assert ss == "/tmp/yocto-build/build/sstate-cache"

    def test_infer_yocto_topdir_default_artifact_dirs(self, deploy_registry_file):
        from bsp.bsp_manager import BspManager
        mgr = BspManager(config_path=str(deploy_registry_file))
        assert mgr._infer_yocto_topdir("/bp", ["tmp/deploy/images"]) == "/bp"

    def test_infer_yocto_topdir_nested_build_prefix(self, deploy_registry_file):
        from bsp.bsp_manager import BspManager
        mgr = BspManager(config_path=str(deploy_registry_file))
        result = mgr._infer_yocto_topdir("/bp", ["build/tmp/deploy/images"])
        assert result == "/bp/build"

    def test_infer_yocto_topdir_no_tmp_segment(self, deploy_registry_file):
        from bsp.bsp_manager import BspManager
        mgr = BspManager(config_path=str(deploy_registry_file))
        # No tmp → fall back to build_path
        result = mgr._infer_yocto_topdir("/bp", ["some/other/path"])
        assert result == "/bp"

    def test_resolve_cache_paths_prefers_env_values_over_default(
        self, deploy_registry_file
    ):
        from bsp.bsp_manager import BspManager
        from bsp.models import YoctoCacheConfig

        mgr = BspManager(config_path=str(deploy_registry_file))
        mgr.initialize()
        mock_env = MagicMock()
        mock_env.get_value.side_effect = lambda key: "/env/dl" if key == "DL_DIR" else "/env/ss"
        mgr.env_manager = mock_env

        cfg = DeployConfig(
            yocto_cache=YoctoCacheConfig(
                enabled=True,
                downloads=True,
                sstate=True,
            )
        )
        dl, ss = mgr._resolve_cache_paths(cfg, build_path="/tmp/yocto-build")

        assert dl == "/env/dl"
        assert ss == "/env/ss"


# =============================================================================
# HTML index generation tests
# =============================================================================


class _SigningBackend(_FakeBackend):
    """Fake backend that supports signed URLs and records uploaded content."""

    def __init__(self, dry_run=False):
        super().__init__(dry_run=dry_run)
        self.contents: dict = {}  # remote_path → file content

    def upload_file(self, local_path, remote_path):
        url = super().upload_file(local_path, remote_path)
        if not self.dry_run:
            self.contents[remote_path] = Path(local_path).read_text()
        return url

    def get_signed_url(self, remote_path, expiry=None):
        if self.dry_run:
            return f"dry-run:{remote_path}"
        return f"https://fake/{remote_path}?sig=TOKEN&se={expiry}"


def _make_result(tmp_path, names=("image.wic.gz", "sdk.tar.gz")):
    result = DeployResult()
    for i, name in enumerate(names):
        local = tmp_path / name
        local.write_bytes(b"x")
        result.artifacts.append(
            UploadedArtifact(
                local_path=local,
                remote_url=f"fake://p/{name}",
                size_bytes=1024 * (i + 1),
                sha256="a" * 64,
            )
        )
    return result


class TestIndexConfigModel:
    def test_defaults(self):
        from bsp.models import IndexConfig
        cfg = IndexConfig()
        assert cfg.enabled is False
        assert cfg.sign_urls is True
        assert cfg.root_index is True
        assert cfg.sas_expiry == "2038-01-19T03:14:06Z"
        assert cfg.tree is True
        assert cfg.collapse_depth == 1
        assert cfg.search is True
        assert cfg.exclude == []
        assert cfg.show_dates is True

    def test_deploy_config_index_default_none(self, default_deploy_config):
        assert default_deploy_config.index is None

    def test_registry_yaml_parsing(self, tmp_path):
        from bsp.utils import get_registry_from_yaml_file
        yaml_text = """
specification:
  version: '2.2'
deploy:
  provider: azure
  container: bsp-artifacts
  index:
    enabled: true
    title: "My {device}"
    sign_urls: false
    root_index: false
registry:
  devices: []
  releases: []
  features: []
  frameworks: []
  distro: []
  vendors: []
"""
        path = tmp_path / "registry.yaml"
        path.write_text(yaml_text)
        root = get_registry_from_yaml_file(path)
        assert root.deploy.index.enabled is True
        assert root.deploy.index.title == "My {device}"
        assert root.deploy.index.sign_urls is False
        assert root.deploy.index.root_index is False


class TestIndexGeneration:
    def _deployer(self, backend=None, **index_kwargs):
        from bsp.models import IndexConfig
        cfg = DeployConfig(
            container="c",
            index=IndexConfig(enabled=True, root_index=False, **index_kwargs),
        )
        return ArtifactDeployer(cfg, backend or _SigningBackend())

    def test_one_row_per_artifact(self, tmp_path):
        prefix = "acme/board/scarthgap/2026-01-01"
        deployer = self._deployer()
        result = _make_result(tmp_path)
        deployer.backend.uploaded = {
            f"{prefix}/{art.local_path.name}": art.local_path
            for art in result.artifacts
        }
        deployer._upload_index(result, prefix)
        uploaded = deployer.backend.uploaded
        assert "index.html" in uploaded
        assert not any(
            key.endswith("/index.html") for key in uploaded
        )
        html_text = deployer.backend.contents["index.html"]
        assert html_text.count("<tr><td><a href=") == 2
        assert "image.wic.gz" in html_text
        assert "sdk.tar.gz" in html_text

    def test_short_sha_included(self, tmp_path):
        deployer = self._deployer()
        result = _make_result(tmp_path, names=("a.wic",))
        html_text = deployer.generate_index_html(
            [{"name": "a.wic", "href": "a.wic", "size_bytes": 10,
              "sha256": "b" * 64}],
            title="t",
        )
        assert "<code>bbbbbbbbbbbb</code>" in html_text

    def test_only_index_pages_excluded(self, tmp_path):
        deployer = self._deployer()
        result = _make_result(tmp_path, names=("image.wic", "index.html", "report.html"))
        deployer.backend.uploaded = {
            "p/image.wic": Path("a"),
            "p/index.html": Path("i"),
            "p/report.html": Path("r"),
        }
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert "image.wic" in html_text
        # genuine HTML build artifacts stay listed, only index pages are skipped
        assert "report.html" in html_text
        assert '"name": "index.html"' not in html_text

    def test_html_escaping(self):
        deployer = self._deployer()
        html_text = deployer.generate_index_html(
            [{"name": "<script>x</script>", "href": 'a"b', "size_bytes": 1,
              "sha256": ""}],
            title="<b>title</b>",
            metadata={"device": "<evil>"},
        )
        assert "<script>x</script>" not in html_text
        assert "&lt;script&gt;" in html_text
        assert "&lt;b&gt;title&lt;/b&gt;" in html_text
        assert "&lt;evil&gt;" in html_text
        assert 'href="a&quot;b"' in html_text

    def test_signed_hrefs_used(self, tmp_path):
        deployer = self._deployer()
        result = _make_result(tmp_path, names=("image.wic",))
        deployer.backend.uploaded = {"p/image.wic": Path("a")}
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert "sig=TOKEN" in html_text

    def test_relative_hrefs_when_signing_disabled(self, tmp_path):
        deployer = self._deployer(sign_urls=False)
        result = _make_result(tmp_path, names=("image.wic",))
        deployer.backend.uploaded = {"p/image.wic": Path("a")}
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert 'href="p/image.wic"' in html_text
        assert "sig=TOKEN" not in html_text

    def test_unsupported_signing_falls_back_to_relative(self, tmp_path):
        class _Unsigned(_SigningBackend):
            def get_signed_url(self, remote_path, expiry=None):
                raise NotImplementedError

        deployer = self._deployer(backend=_Unsigned())
        result = _make_result(tmp_path, names=("image.wic",))
        deployer.backend.uploaded = {"p/image.wic": Path("a")}
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert 'href="p/image.wic"' in html_text

    def test_no_cache_meta_tags(self, tmp_path):
        deployer = self._deployer()
        html_text = deployer.generate_index_html([], title="t")
        assert "no-cache" in html_text
        assert 'http-equiv="Pragma"' in html_text

    def test_dry_run_requires_no_credentials(self, tmp_path, capsys):
        deployer = self._deployer(backend=_SigningBackend(dry_run=True))
        result = _make_result(tmp_path, names=("image.wic",))
        url = deployer._upload_index(result, "p")
        assert url == "dry-run:index.html"
        assert "[dry-run]" in capsys.readouterr().out

    def test_index_upload_failure_does_not_raise(self, tmp_path):
        class _Boom(_SigningBackend):
            def upload_file(self, local_path, remote_path):
                raise RuntimeError("nope")

        deployer = self._deployer(backend=_Boom())
        result = _make_result(tmp_path, names=("image.wic",))
        assert deployer._upload_index(result, "p") is None

    def test_deploy_generates_index_when_enabled(self, tmp_path):
        from bsp.models import IndexConfig
        images = tmp_path / "tmp/deploy/images"
        images.mkdir(parents=True)
        (images / "core-image.wic").write_bytes(b"1234")

        cfg = DeployConfig(
            container="c",
            prefix="v/d/r/2026-01-01",
            index=IndexConfig(enabled=True, root_index=False),
        )
        backend = _SigningBackend()
        result = ArtifactDeployer(cfg, backend).deploy(str(tmp_path))
        assert result.index_url == "fake://index.html"
        assert "index.html" in backend.uploaded
        assert "v/d/r/2026-01-01/index.html" not in backend.uploaded

    def test_deploy_skips_index_by_default(self, tmp_path):
        images = tmp_path / "tmp/deploy/images"
        images.mkdir(parents=True)
        (images / "core-image.wic").write_bytes(b"1234")

        cfg = DeployConfig(container="c", prefix="v/d/r/2026-01-01")
        backend = _SigningBackend()
        result = ArtifactDeployer(cfg, backend).deploy(str(tmp_path))
        assert result.index_url is None
        assert not any(k.endswith("index.html") for k in backend.uploaded)

    def test_update_index_argument_overrides_config(self, tmp_path):
        images = tmp_path / "tmp/deploy/images"
        images.mkdir(parents=True)
        (images / "core-image.wic").write_bytes(b"1234")

        cfg = DeployConfig(container="c", prefix="p")
        backend = _SigningBackend()
        ArtifactDeployer(cfg, backend).deploy(str(tmp_path), update_index=True)
        assert "index.html" in backend.uploaded
        assert "p/index.html" not in backend.uploaded

    def test_deploy_refreshes_whole_container_index(self, tmp_path):
        """Deploying must refresh the root index, like `bsp deploy index` does."""
        from bsp.models import IndexConfig
        images = tmp_path / "tmp/deploy/images"
        images.mkdir(parents=True)
        (images / "core-image.wic").write_bytes(b"1234")

        backend = _SigningBackend()
        backend.uploaded = {"old/prefix/a.wic": Path("a")}
        cfg = DeployConfig(
            container="c", prefix="v/d/r/2026-01-01",
            index=IndexConfig(enabled=True),
        )
        ArtifactDeployer(cfg, backend).deploy(str(tmp_path))
        assert "index.html" in backend.uploaded
        assert not any(k.endswith("/index.html") for k in backend.uploaded)
        page = backend.contents["index.html"]
        assert "old/prefix/a.wic" in page
        assert "v/d/r/2026-01-01/core-image.wic" in page

    def test_refresh_container_indexes_writes_only_root_page(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {"a/x.wic": Path("x"), "b/y.wic": Path("y")}
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        urls = ArtifactDeployer(cfg, backend).refresh_container_indexes()
        assert set(urls) == {"index.html"}
        assert "a/index.html" not in backend.uploaded
        assert "b/index.html" not in backend.uploaded
        page = backend.contents["index.html"]
        assert "a/x.wic" in page
        assert "b/y.wic" in page

    def test_root_index_groups_by_prefix(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {
            "acme/board/scarthgap/2026-01-01/a.wic": Path("a"),
            "acme/board/scarthgap/2026-02-01/b.wic": Path("b"),
            "other/dev/kirkstone/2025-01-01/c.wic": Path("c"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        deployer = ArtifactDeployer(cfg, backend)
        deployer._upload_root_index()
        html_text = backend.contents["index.html"]
        assert "acme/board/scarthgap/2026-02-01" in html_text
        assert "other/dev/kirkstone/2025-01-01" in html_text
        # newest (lexicographically greatest) prefix first
        assert html_text.index("2026-02-01") < html_text.index("2026-01-01")

    def test_rebuild_index_from_listing(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {
            "p/a.wic": Path("a"),
            "p/manifest.json": Path("m"),
            "p/index.html": Path("i"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        deployer = ArtifactDeployer(cfg, backend)
        deployer.rebuild_index()
        html_text = backend.contents["index.html"]
        assert "a.wic" in html_text
        assert "manifest.json" in html_text
        assert html_text.count("<tr><td><a href=") == 2

    def test_rebuild_index_ignores_prefix_argument(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {"p/a.wic": Path("a"), "q/b.wic": Path("b")}
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        url = ArtifactDeployer(cfg, backend).rebuild_index("p")
        assert url == "fake://index.html"
        assert "p/index.html" not in backend.uploaded
        page = backend.contents["index.html"]
        assert "p/a.wic" in page
        assert "q/b.wic" in page


class TestIndexTree:
    """Directory-preserving tree model and renderer."""

    def _deployer(self, backend=None, **index_kwargs):
        from bsp.models import IndexConfig
        cfg = DeployConfig(
            container="c",
            index=IndexConfig(enabled=True, root_index=False, **index_kwargs),
        )
        return ArtifactDeployer(cfg, backend or _SigningBackend())

    def test_build_tree_nests_directories(self):
        from bsp.deployer import build_index_tree
        tree = build_index_tree([
            {"path": "images/a.wic", "size_bytes": 10},
            {"path": "images/deep/b.wic", "size_bytes": 5},
            {"path": "top.txt", "size_bytes": 1},
        ])
        assert tree["file_count"] == 3
        assert tree["size_bytes"] == 16
        names = [c["name"] for c in tree["children"]]
        assert names == ["images", "top.txt"]
        images = tree["children"][0]
        assert images["type"] == "dir"
        assert images["file_count"] == 2
        assert images["size_bytes"] == 15
        deep = [c for c in images["children"] if c["type"] == "dir"][0]
        assert deep["path"] == "images/deep"
        assert deep["children"][0]["path"] == "images/deep/b.wic"

    def test_build_tree_keeps_same_named_files_distinct(self):
        from bsp.deployer import build_index_tree, flatten_index_tree
        tree = build_index_tree([
            {"path": "a/img.wic", "size_bytes": 1},
            {"path": "b/img.wic", "size_bytes": 2},
        ])
        paths = sorted(f["path"] for f in flatten_index_tree(tree))
        assert paths == ["a/img.wic", "b/img.wic"]

    def test_rebuild_index_preserves_structure(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {
            "p/images/a.wic": Path("a"),
            "p/sdk/deep/b.sh": Path("b"),
            "p/manifest.json": Path("m"),
            "p/index.html": Path("i"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        ArtifactDeployer(cfg, backend).rebuild_index()
        html_text = backend.contents["index.html"]
        assert '"path": "p/images/a.wic"' in html_text
        assert '"path": "p/sdk/deep/b.sh"' in html_text
        assert '"name": "index.html"' not in html_text
        assert "manifest.json" in html_text

    def test_relative_hrefs_keep_directory_component(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {"p/images/a.wic": Path("a")}
        cfg = DeployConfig(
            container="c", index=IndexConfig(enabled=True, sign_urls=False)
        )
        ArtifactDeployer(cfg, backend).rebuild_index()
        html_text = backend.contents["index.html"]
        assert '"href": "p/images/a.wic"' in html_text

    def test_exclude_patterns_drop_paths(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {
            "p/images/a.wic": Path("a"),
            "p/cache/downloads.tar.gz": Path("c"),
        }
        cfg = DeployConfig(
            container="c",
            index=IndexConfig(enabled=True, exclude=["*/cache/*"]),
        )
        ArtifactDeployer(cfg, backend).rebuild_index()
        html_text = backend.contents["index.html"]
        assert "a.wic" in html_text
        assert "downloads.tar.gz" not in html_text

    def test_tree_page_has_controls_and_data_island(self, tmp_path):
        deployer = self._deployer()
        result = _make_result(tmp_path)
        deployer.backend.uploaded = {"p/image.wic": Path("a")}
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert 'id="bsp-index-data" type="application/json"' in html_text
        assert 'id="bsp-search"' in html_text
        assert 'aria-expanded' in html_text
        assert 'id="bsp-expand"' in html_text
        assert "<noscript>" in html_text

    def test_no_search_controls_when_disabled(self, tmp_path):
        deployer = self._deployer(search=False)
        result = _make_result(tmp_path)
        deployer.backend.uploaded = {"p/image.wic": Path("a")}
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert 'id="bsp-search"' not in html_text
        assert 'data-ext=' not in html_text

    def test_flat_mode_matches_legacy_table(self, tmp_path):
        deployer = self._deployer(tree=False)
        result = _make_result(tmp_path)
        deployer.backend.uploaded = {
            f"p/{art.local_path.name}": art.local_path
            for art in result.artifacts
        }
        deployer._upload_index(result, "p")
        html_text = deployer.backend.contents["index.html"]
        assert "bsp-index-data" not in html_text
        assert html_text.count("<tr><td><a href=") == 2

    def test_json_island_escapes_markup(self):
        deployer = self._deployer()
        entries = [{
            "name": "</script><script>alert(1)</script>",
            "path": "</script><script>alert(1)</script>",
            "href": "a\"b",
            "size_bytes": 1,
            "sha256": "",
        }]
        from bsp.deployer import build_index_tree
        html_text = deployer.generate_index_html(
            entries, title="t", tree=build_index_tree(entries)
        )
        assert "</script><script>alert(1)" not in html_text
        assert "\\u003c/script\\u003e" in html_text
        assert 'href="a&quot;b"' in html_text

    def test_soft_limit_warning(self, caplog):
        from bsp.models import IndexConfig
        import logging as _logging
        backend = _SigningBackend()
        backend.uploaded = {f"p/f{i}.bin": Path("x") for i in range(5001)}
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        with caplog.at_level(_logging.WARNING):
            ArtifactDeployer(cfg, backend).rebuild_index()
        assert any("soft limit" in r.message for r in caplog.records)

    def test_root_index_tree_is_navigable(self):
        from bsp.models import IndexConfig
        backend = _SigningBackend()
        backend.uploaded = {
            "acme/board/scarthgap/2026-01-01/a.wic": Path("a"),
            "acme/board/scarthgap/2026-02-01/b.wic": Path("b"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        ArtifactDeployer(cfg, backend)._upload_root_index()
        html_text = backend.contents["index.html"]
        assert '"name": "acme"' in html_text
        assert '"path": "acme/board/scarthgap/2026-02-01/b.wic"' in html_text


class TestDetailedListing:
    def test_base_fallback_reports_unknown_metadata(self):
        backend = _FakeBackend()
        backend.uploaded = {"p/a.wic": Path("a")}
        records = backend.list_artifacts_detailed("p")
        assert records == [
            {"path": "p/a.wic", "size": None, "last_modified": None, "etag": None}
        ]

    def test_detailed_listing_used_for_sizes(self):
        from bsp.models import IndexConfig

        class _Detailed(_SigningBackend):
            def list_artifacts_detailed(self, prefix):
                return [{
                    "path": "p/a.wic", "size": 2048,
                    "last_modified": "2026-01-01T00:00:00+00:00", "etag": "e",
                }]

        backend = _Detailed()
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        ArtifactDeployer(cfg, backend).rebuild_index()
        html_text = backend.contents["index.html"]
        assert '"size_bytes": 2048' in html_text
        assert "2026-01-01T00:00:00+00:00" in html_text


class TestIndexMisc:
    def test_human_size(self):
        from bsp.deployer import human_size
        assert human_size(512) == "512 B"
        assert human_size(2048) == "2.0 KiB"
        assert human_size(5 * 1024 * 1024) == "5.0 MiB"


class TestAzureSignedUrls:
    def _sdk_or_skip(self):
        try:
            import azure.storage.blob  # noqa: F401
        except ImportError:
            pytest.skip("azure-storage-blob not installed")

    def test_dry_run_signed_url_no_credentials(self):
        from bsp.storage.azure import AzureStorageBackend
        backend = AzureStorageBackend(container_name="c", dry_run=True)
        assert backend.get_signed_url("p/a.wic") == "dry-run:p/a.wic"

    def test_parse_expiry_default(self):
        from bsp.storage.azure import AzureStorageBackend, DEFAULT_SAS_EXPIRY
        dt = AzureStorageBackend._parse_expiry(None)
        assert dt.year == 2038
        assert DEFAULT_SAS_EXPIRY.startswith("2038")

    def test_parse_account_key(self):
        from bsp.storage.azure import AzureStorageBackend
        key = AzureStorageBackend._parse_account_key(
            "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=SECRET==;"
        )
        assert key == "SECRET=="

    def test_account_key_sas_uses_requested_expiry(self):
        self._sdk_or_skip()
        from bsp.storage.azure import AzureStorageBackend

        mock_service = MagicMock()
        mock_service.url = "https://myaccount.blob.core.windows.net"
        mock_service.account_name = "myaccount"

        with patch("azure.storage.blob.BlobServiceClient") as mock_cls:
            mock_cls.from_connection_string.return_value = mock_service
            backend = AzureStorageBackend(
                container_name="c",
                connection_string="AccountName=myaccount;AccountKey=a2V5MTIz;",
            )
        with patch("azure.storage.blob.generate_blob_sas", return_value="tok") as gen:
            url = backend.get_signed_url("p/a.wic")
        assert url.endswith("?tok")
        assert gen.call_args.kwargs["expiry"].year == 2038
        mock_service.get_user_delegation_key.assert_not_called()

    def test_user_delegation_expiry_clamped(self):
        self._sdk_or_skip()
        import datetime as _dt
        from bsp.storage.azure import AzureStorageBackend, MAX_USER_DELEGATION_DAYS

        mock_service = MagicMock()
        mock_service.url = "https://myaccount.blob.core.windows.net"
        mock_service.account_name = "myaccount"

        with patch("azure.storage.blob.BlobServiceClient", return_value=mock_service):
            backend = AzureStorageBackend(
                container_name="c",
                account_url="https://myaccount.blob.core.windows.net",
                credential=MagicMock(),
            )
        with patch("azure.storage.blob.generate_blob_sas", return_value="tok") as gen:
            backend.get_signed_url("p/a.wic")

        mock_service.get_user_delegation_key.assert_called_once()
        expiry = gen.call_args.kwargs["expiry"]
        limit = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(
            days=MAX_USER_DELEGATION_DAYS
        )
        assert expiry <= limit + _dt.timedelta(minutes=1)

    def test_content_settings_html(self):
        self._sdk_or_skip()
        from bsp.storage.azure import AzureStorageBackend
        backend = AzureStorageBackend(container_name="c", dry_run=True)
        cs = backend._content_settings_for("p/index.html")
        assert cs.content_type == "text/html"

    def test_content_settings_wic_gz_no_encoding(self):
        self._sdk_or_skip()
        from bsp.storage.azure import AzureStorageBackend
        backend = AzureStorageBackend(container_name="c", dry_run=True)
        cs = backend._content_settings_for("p/image.wic.gz")
        assert cs.content_type == "application/octet-stream"
        assert not cs.content_encoding

    def test_base_backend_signed_url_not_implemented(self):
        backend = _FakeBackend()
        with pytest.raises(NotImplementedError):
            backend.get_signed_url("p/a.wic")


class TestIndexCli:
    """CLI wiring tests (the parser is built inside ``bsp.cli.main``)."""

    def _help(self, *command):
        from bsp.cli import main
        with patch.object(sys, "argv", ["bsp", *command, "--help"]):
            try:
                main()
            except SystemExit:
                pass

    def test_deploy_has_update_index_flags(self, capsys):
        self._help("deploy")
        out = capsys.readouterr().out
        assert "--update-index" in out
        assert "--no-update-index" in out

    def test_build_has_update_index_flags(self, capsys):
        self._help("build")
        out = capsys.readouterr().out
        assert "--update-index" in out

    def test_index_subcommand_help(self, capsys):
        self._help("deploy", "index")
        out = capsys.readouterr().out
        assert "bsp deploy index" in out
        assert "--prefix" in out
        assert "--root" in out
        assert "--no-sign-urls" in out

    def test_index_not_a_top_level_command(self, capsys):
        self._help()
        out = capsys.readouterr().out
        assert "deploy" in out
        assert "\n    index" not in out

    def test_index_tree_flags(self, capsys):
        self._help("deploy", "index")
        out = capsys.readouterr().out
        assert "--flat" in out
        assert "--collapse-depth" in out
        assert "--exclude" in out
        assert "--no-search" in out

    def test_index_command_forwards_tree_options(self):
        import bsp.cli as cli
        args = MagicMock()
        args.container = "c"
        args.deploy_provider = "azure"
        args.dry_run = True
        args.index_sign_urls = True
        args.index_sas_expiry = None
        args.index_root = False
        args.index_prefix = "p"
        args.index_account_url = None
        args.index_tree = False
        args.index_collapse_depth = 3
        args.index_search = False
        args.index_exclude = ["cache/*"]

        captured = {}

        class _Deployer:
            def __init__(self, cfg, backend):
                captured["cfg"] = cfg

            def refresh_container_indexes(self, index_config=None, skip_prefixes=None):
                captured["index_config"] = index_config
                return {}

        with patch("bsp.deployer.ArtifactDeployer", _Deployer), \
                patch("bsp.storage.create_backend", return_value=object()):
            assert cli._run_index_command(args) == 0

        cfg = captured["index_config"]
        assert cfg.tree is False
        assert cfg.collapse_depth == 3
        assert cfg.search is False
        assert cfg.exclude == ["cache/*"]

    def test_index_command_refreshes_whole_container(self):
        """`bsp deploy index` always rebuilds the container-root index."""
        import bsp.cli as cli
        from bsp.models import IndexConfig

        args = MagicMock()
        args.container = "c"
        args.deploy_provider = "azure"
        args.dry_run = False
        args.index_sign_urls = False
        args.index_sas_expiry = None
        args.index_root = True
        args.index_prefix = None
        args.index_account_url = None
        args.index_tree = None
        args.index_collapse_depth = None
        args.index_search = None
        args.index_exclude = None
        args.index_facets = None
        args.index_no_facets = False
        args.index_theme = None
        args.index_accent = None

        backend = _SigningBackend()
        backend.uploaded = {"a/x.wic": Path("x"), "b/y.wic": Path("y")}
        deploy_cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        deployer = ArtifactDeployer(deploy_cfg, backend)

        with patch("bsp.deployer.ArtifactDeployer", return_value=deployer), \
                patch("bsp.storage.create_backend", return_value=backend):
            assert cli._run_index_command(args) == 0

        assert "a/index.html" not in backend.uploaded
        assert "b/index.html" not in backend.uploaded
        assert "index.html" in backend.uploaded
        page = backend.contents["index.html"]
        assert "a/x.wic" in page
        assert "b/y.wic" in page

    def test_index_command_uses_registry_deploy_config(self):
        import bsp.cli as cli
        from bsp.models import DeployConfig, IndexConfig

        args = MagicMock()
        args.container = None
        args.deploy_provider = None
        args.dry_run = True
        args.index_sign_urls = None
        args.index_sas_expiry = None
        args.index_root = False
        args.index_prefix = None
        args.index_account_url = None
        args.index_tree = None
        args.index_collapse_depth = None
        args.index_search = None
        args.index_exclude = None
        args.index_facets = None
        args.index_no_facets = False
        args.index_theme = None
        args.index_accent = None

        mgr = MagicMock()
        mgr.get_registry_deploy_config.return_value = DeployConfig(
            provider="azure",
            container="bsp-registry-artifacts",
            account_url="https://modularbsp.blob.core.windows.net",
            index=IndexConfig(enabled=True, theme="dark", collapse_depth=3),
        )

        captured = {}

        class _Deployer:
            def __init__(self, cfg, backend):
                captured["cfg"] = cfg

            def rebuild_index(self, prefix, index_config=None):
                captured["index_config"] = index_config
                return None

            def refresh_container_indexes(self, index_config=None, skip_prefixes=None):
                captured["index_config"] = index_config
                return {}

        def _create_backend(provider, **kwargs):
            captured["provider"] = provider
            captured["kwargs"] = kwargs
            return object()

        with patch("bsp.deployer.ArtifactDeployer", _Deployer), \
                patch("bsp.storage.create_backend", _create_backend):
            assert cli._run_index_command(args, mgr) == 0

        assert captured["provider"] == "azure"
        assert captured["kwargs"]["container_name"] == "bsp-registry-artifacts"
        assert captured["kwargs"]["account_url"] == (
            "https://modularbsp.blob.core.windows.net"
        )
        assert captured["cfg"].container == "bsp-registry-artifacts"
        assert captured["index_config"].theme == "dark"
        assert captured["index_config"].collapse_depth == 3

    def test_index_command_cli_overrides_registry(self):
        import bsp.cli as cli
        from bsp.models import DeployConfig

        args = MagicMock()
        args.container = "cli-container"
        args.deploy_provider = "azure"
        args.dry_run = True
        args.index_sign_urls = None
        args.index_sas_expiry = None
        args.index_root = False
        args.index_prefix = None
        args.index_account_url = "https://cli.blob.core.windows.net"
        args.index_tree = None
        args.index_collapse_depth = None
        args.index_search = None
        args.index_exclude = None
        args.index_facets = None
        args.index_no_facets = False
        args.index_theme = None
        args.index_accent = None

        mgr = MagicMock()
        mgr.get_registry_deploy_config.return_value = DeployConfig(
            provider="aws", bucket="registry-bucket",
            account_url="https://registry.blob.core.windows.net",
        )

        captured = {}

        class _Deployer:
            def __init__(self, cfg, backend):
                pass

            def rebuild_index(self, prefix, index_config=None):
                return None

            def refresh_container_indexes(self, index_config=None, skip_prefixes=None):
                return {}

        def _create_backend(provider, **kwargs):
            captured["provider"] = provider
            captured["kwargs"] = kwargs
            return object()

        with patch("bsp.deployer.ArtifactDeployer", _Deployer), \
                patch("bsp.storage.create_backend", _create_backend):
            assert cli._run_index_command(args, mgr) == 0

        assert captured["provider"] == "azure"
        assert captured["kwargs"]["container_name"] == "cli-container"
        assert captured["kwargs"]["account_url"] == "https://cli.blob.core.windows.net"

    def test_index_command_errors_without_container(self):
        import bsp.cli as cli

        args = MagicMock()
        args.container = None
        args.deploy_provider = None
        args.dry_run = True
        args.index_account_url = None

        mgr = MagicMock()
        mgr.get_registry_deploy_config.return_value = None

        assert cli._run_index_command(args, mgr) == 1

    def test_rewrite_deploy_index_argv(self):
        from bsp.cli import _rewrite_deploy_index_argv
        assert _rewrite_deploy_index_argv(
            ["deploy", "index", "cont", "--root"]
        ) == ["index", "cont", "--root"]
        assert _rewrite_deploy_index_argv(
            ["-v", "--registry", "deploy", "deploy", "index", "cont"]
        ) == ["-v", "--registry", "deploy", "index", "cont"]
        assert _rewrite_deploy_index_argv(
            ["deploy", "mybsp"]
        ) == ["deploy", "mybsp"]
        assert _rewrite_deploy_index_argv(["index", "cont"]) == ["index", "cont"]

    def test_collect_deploy_overrides_index(self):
        from bsp.cli import _collect_deploy_overrides
        from bsp.models import IndexConfig
        args = MagicMock()
        args.deploy_provider = None
        args.deploy_container = None
        args.deploy_prefix = None
        args.deploy_patterns = None
        args.deploy_archive_name = None
        args.deploy_archive_format = None
        args.deploy_cache = None
        args.update_index = True
        # --update-index is forwarded as ArtifactDeployer.deploy(update_index=...)
        # and must not replace the registry-configured IndexConfig.
        assert "index" not in _collect_deploy_overrides(args)
        assert IndexConfig().enabled is not None

    def test_collect_deploy_overrides_no_index_by_default(self):
        from bsp.cli import _collect_deploy_overrides
        args = MagicMock()
        args.deploy_provider = None
        args.deploy_container = None
        args.deploy_prefix = None
        args.deploy_patterns = None
        args.deploy_archive_name = None
        args.deploy_archive_format = None
        args.deploy_cache = None
        args.update_index = None
        assert "index" not in _collect_deploy_overrides(args)

    def test_index_command_dry_run_no_credentials(self, capsys):
        from bsp.cli import _run_index_command
        args = MagicMock()
        args.container = "c"
        args.deploy_provider = "azure"
        args.dry_run = True
        args.index_prefix = "a/b"
        args.index_root = False
        args.index_sign_urls = True
        args.index_sas_expiry = "2038-01-19T03:14:06Z"
        args.index_account_url = None
        assert _run_index_command(args) == 0
        assert "[dry-run]" in capsys.readouterr().out

    def _signed_index_args(self, root=False):
        args = MagicMock()
        args.container = "c"
        args.deploy_provider = "azure"
        args.dry_run = False
        args.index_prefix = "a/b"
        args.index_root = root
        args.index_sign_urls = True
        args.index_sas_expiry = None
        args.index_account_url = None
        return args

    def _run_with_backend(self, args, backend):
        import bsp.cli as cli

        class _Deployer:
            def __init__(self, cfg, be):
                pass

            def refresh_container_indexes(self, index_config=None, skip_prefixes=None):
                return {
                    "index.html": "https://acct.blob.core.windows.net/c/index.html"
                }

        with patch("bsp.deployer.ArtifactDeployer", _Deployer), \
                patch("bsp.storage.create_backend", return_value=backend):
            return cli._run_index_command(args)

    def test_index_command_prints_signed_url(self, capsys):
        class _Backend:
            dry_run = False

            def get_signed_url(self, remote_path, expiry=None):
                return f"https://acct.blob.core.windows.net/c/{remote_path}?sig=TOKEN"

        assert self._run_with_backend(self._signed_index_args(True), _Backend()) == 0
        out = capsys.readouterr().out
        assert (
            "index.html → https://acct.blob.core.windows.net/c/index.html?sig=TOKEN"
            in out
        )

    def test_index_command_falls_back_when_signing_unsupported(self, capsys):
        class _Backend:
            dry_run = False

            def get_signed_url(self, remote_path, expiry=None):
                raise NotImplementedError

        assert self._run_with_backend(self._signed_index_args(), _Backend()) == 0
        out = capsys.readouterr().out
        assert "index.html → https://acct.blob.core.windows.net/c/index.html" in out
        assert "sig=" not in out

    def test_index_command_falls_back_when_signing_fails(self, capsys):
        class _Backend:
            dry_run = False

            def get_signed_url(self, remote_path, expiry=None):
                raise RuntimeError("boom")

        assert self._run_with_backend(self._signed_index_args(), _Backend()) == 0
        assert "index.html" in capsys.readouterr().out

    def test_index_command_no_signing_prints_plain_url(self, capsys):
        class _Backend:
            dry_run = False

            def get_signed_url(self, remote_path, expiry=None):
                raise AssertionError("should not be called")

        args = self._signed_index_args()
        args.index_sign_urls = False
        assert self._run_with_backend(args, _Backend()) == 0
        assert "sig=" not in capsys.readouterr().out


# =============================================================================
# Faceted filtering and styling of the generated index
# =============================================================================


class _DownloadingBackend(_SigningBackend):
    """Signing backend that can serve back previously uploaded content."""

    def download_file(self, remote_path, local_path):
        if remote_path not in self.contents:
            raise FileNotFoundError(remote_path)
        Path(local_path).write_text(self.contents[remote_path])


class TestPrefixFacetParsing:
    def test_default_template_roundtrip(self):
        from bsp.deployer import parse_prefix_facets
        facets = parse_prefix_facets(
            "{vendor}/{device}/{release}/{date}",
            "acme/board/scarthgap/2026-01-01",
        )
        assert facets["vendor"] == "acme"
        assert facets["device"] == "board"
        assert facets["machine"] == "board"
        assert facets["release"] == "scarthgap"
        assert facets["date"] == "2026-01-01"

    def test_non_matching_prefix_returns_empty(self):
        from bsp.deployer import parse_prefix_facets
        assert parse_prefix_facets("{vendor}/{device}", "only-one") == {}

    def test_literal_separators_respected(self):
        from bsp.deployer import parse_prefix_facets
        facets = parse_prefix_facets("builds/{device}-{release}", "builds/rpi4-kirkstone")
        assert facets["device"] == "rpi4"
        assert facets["release"] == "kirkstone"

    def test_empty_inputs(self):
        from bsp.deployer import parse_prefix_facets
        assert parse_prefix_facets("", "") == {}
        assert parse_prefix_facets("no-placeholders", "no-placeholders") == {}


class TestFacetCollection:
    def _tree(self):
        from bsp.deployer import build_index_tree
        return build_index_tree([
            {"path": "a/x.wic", "facets": {"machine": "rpi4", "release": "scarthgap"}},
            {"path": "a/y.wic", "facets": {"machine": "rpi4", "release": "scarthgap"}},
            {"path": "b/z.wic", "facets": {"machine": "qemu", "release": "kirkstone"}},
        ])

    def test_directory_inherits_child_facets(self):
        tree = self._tree()
        top = {c["name"]: c for c in tree["children"]}
        assert top["a"]["facets"]["machine"] == ["rpi4"]
        assert sorted(tree["facets"]["machine"]) == ["qemu", "rpi4"]

    def test_counts_and_labels(self):
        from bsp.deployer import collect_facets
        groups = collect_facets(self._tree(), ["machine", "release"])
        by_key = {g["key"]: g for g in groups}
        assert by_key["machine"]["label"] == "Machine"
        assert by_key["machine"]["values"][0] == {"value": "rpi4", "count": 2}

    def test_unknown_and_empty_groups_dropped(self):
        from bsp.deployer import collect_facets
        groups = collect_facets(self._tree(), ["bogus", "preset", "machine"])
        assert [g["key"] for g in groups] == ["machine"]


class TestIndexFacetPage:
    def _deploy(self, tmp_path, backend, **index_kwargs):
        from bsp.models import IndexConfig
        (tmp_path / "tmp/deploy/images").mkdir(parents=True)
        (tmp_path / "tmp/deploy/images/core-image.wic").write_bytes(b"x")
        cfg = DeployConfig(
            container="c",
            index=IndexConfig(enabled=True, sign_urls=False, **index_kwargs),
        )
        deployer = ArtifactDeployer(cfg, backend)
        deployer.deploy(
            str(tmp_path), device="board", release="scarthgap",
            distro="poky", vendor="acme", preset="my-preset",
        )
        return deployer

    def test_facets_rendered_and_embedded(self, tmp_path):
        backend = _DownloadingBackend()
        self._deploy(tmp_path, backend)
        page = backend.contents["index.html"]
        assert 'id="bsp-facets"' in page
        assert 'data-facet="preset"' in page
        assert 'data-value="my-preset"' in page
        assert 'data-facet="machine"' in page
        assert 'data-facet="release"' in page
        assert 'data-bucket="today"' in page
        assert 'id="bsp-date-from"' in page

    def test_upload_date_recorded(self, tmp_path):
        backend = _DownloadingBackend()
        self._deploy(tmp_path, backend)
        page = backend.contents["index.html"]
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        assert today in page

    def test_facets_disabled_by_config(self, tmp_path):
        backend = _DownloadingBackend()
        self._deploy(tmp_path, backend, facets=[])
        page = backend.contents["index.html"]
        assert 'id="bsp-facets"' not in page

    def test_index_meta_sidecar_written(self, tmp_path):
        backend = _DownloadingBackend()
        self._deploy(tmp_path, backend)
        remote = next(k for k in backend.contents if k.endswith("index-meta.json"))
        meta = json.loads(backend.contents[remote])
        assert meta["facets"]["preset"] == "my-preset"
        assert meta["facets"]["machine"] == "board"
        assert meta["facets"]["release"] == "scarthgap"
        assert meta["uploaded_at"]

    def test_index_meta_roundtrip_on_rebuild(self, tmp_path):
        from bsp.models import IndexConfig
        backend = _DownloadingBackend()
        deployer = self._deploy(tmp_path, backend)
        deployer.rebuild_index(index_config=IndexConfig(enabled=True, sign_urls=False))
        page = backend.contents["index.html"]
        assert 'data-value="my-preset"' in page
        assert "index-meta.json" not in page.split('id="bsp-index-data"')[0]

    def test_rebuild_matches_deploy_title_and_badges(self, tmp_path):
        """`bsp deploy index` must render the same header as `--update-index`."""
        from bsp.models import IndexConfig
        backend = _DownloadingBackend()
        deployer = self._deploy(tmp_path, backend)
        deployed = backend.contents["index.html"]
        deployer.rebuild_index(
            index_config=IndexConfig(enabled=True, sign_urls=False)
        )
        rebuilt = backend.contents["index.html"]

        def header(page):
            return page.split('<dl class="badges">')[0], page.split(
                '<dl class="badges">'
            )[1].split("</dl>")[0]

        deployed_title, deployed_badges = header(deployed)
        rebuilt_title, rebuilt_badges = header(rebuilt)
        assert "<h1>" in rebuilt_title
        assert deployed_title.split("<h1>")[1] == rebuilt_title.split("<h1>")[1]
        # "generated" timestamps legitimately differ; compare the other badges.
        strip = lambda badges: [  # noqa: E731
            b for b in badges.splitlines() if "<dt>generated</dt>" not in b
        ]
        assert strip(deployed_badges) == strip(rebuilt_badges)

    def test_facet_values_are_html_escaped(self):
        from bsp.deployer import build_index_tree
        from bsp.models import IndexConfig
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        entries = [{
            "name": "a.wic", "path": "a.wic", "href": "a.wic",
            "size_bytes": 1, "sha256": "",
            "facets": {"preset": '<img src=x onerror=alert(1)>'},
        }]
        page = deployer.generate_index_html(
            entries, title="t", tree=build_index_tree(entries),
        )
        assert "<img src=x" not in page
        assert "&lt;img src=x" in page

    def test_no_external_resources(self, tmp_path):
        backend = _DownloadingBackend()
        self._deploy(tmp_path, backend)
        page = backend.contents["index.html"]
        assert "http://" not in page
        assert "https://" not in page
        assert "<noscript>" in page


class TestIndexStyling:
    def _page(self, **index_kwargs):
        from bsp.deployer import build_index_tree
        from bsp.models import IndexConfig
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True, **index_kwargs))
        deployer = ArtifactDeployer(cfg, _FakeBackend())
        entries = [{
            "name": "a.wic", "path": "dir/a.wic", "href": "dir/a.wic",
            "size_bytes": 10, "sha256": "b" * 64,
        }]
        return deployer.generate_index_html(
            entries, title="t", metadata={"prefix": "acme/board"},
            tree=build_index_tree(entries),
        )

    def test_design_tokens_and_dark_mode(self):
        page = self._page()
        assert "--accent:" in page
        assert "prefers-color-scheme: dark" in page

    def test_theme_attribute(self):
        assert 'data-theme="dark"' in self._page(theme="dark")
        assert 'data-theme="auto"' in self._page(theme="bogus")

    def test_accent_override(self):
        assert "--accent: #ff0000" in self._page(accent="#ff0000")

    def test_hostile_accent_is_dropped(self):
        page = self._page(accent="red; } body { background: url(http://evil) }")
        assert "evil" not in page
        assert "--accent: red" not in page

    def test_badges_and_breadcrumb(self):
        page = self._page()
        assert 'class="badges"' in page
        assert 'class="breadcrumb"' in page
        assert "../index.html" in page

    def test_summary_element_present(self):
        assert 'id="bsp-summary"' in self._page()


class TestRootIndexBuildBrowser:
    def test_prefix_rows_carry_facets(self):
        from bsp.models import IndexConfig
        backend = _DownloadingBackend()
        backend.uploaded = {
            "acme/board/scarthgap/2026-01-01/a.wic": Path("a"),
            "other/dev/kirkstone/2025-01-01/c.wic": Path("c"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        deployer = ArtifactDeployer(cfg, backend)
        deployer._upload_root_index()
        page = backend.contents["index.html"]
        assert 'data-facet="machine"' in page
        assert 'data-value="board"' in page
        assert 'data-value="scarthgap"' in page

    def test_newest_prefix_first(self):
        from bsp.models import IndexConfig
        backend = _DownloadingBackend()
        backend.uploaded = {
            "acme/board/scarthgap/2026-01-01/a.wic": Path("a"),
            "acme/board/scarthgap/2026-02-01/b.wic": Path("b"),
        }
        cfg = DeployConfig(container="c", index=IndexConfig(enabled=True))
        ArtifactDeployer(cfg, backend)._upload_root_index()
        page = backend.contents["index.html"]
        assert page.index("2026-02-01") < page.index("2026-01-01")


class TestIndexConfigFacetDefaults:
    def test_defaults(self):
        from bsp.models import IndexConfig
        cfg = IndexConfig()
        assert cfg.facets == ["preset", "machine", "release", "date"]
        assert cfg.theme == "auto"
        assert cfg.accent == ""


# =============================================================================
# Build manifest upload
# =============================================================================


class TestBuildManifestUpload:
    """``build-manifest.json`` written by ``bsp build`` is deployed too."""

    @staticmethod
    def _prepare(tmp_path, write_manifest=True):
        deploy_dir = tmp_path / "tmp" / "deploy" / "images"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "image.wic.gz").write_bytes(b"data")
        if write_manifest:
            (tmp_path / "build-manifest.json").write_text('{"schema_version": "1"}')

    @staticmethod
    def _config(**kwargs):
        base = dict(
            artifact_dirs=["tmp/deploy/images"],
            patterns=["**/*.wic.gz"],
            include_manifest=False,
            prefix="acme/board/rel",
        )
        base.update(kwargs)
        return DeployConfig(**base)

    def test_default_is_enabled(self, default_deploy_config):
        assert default_deploy_config.include_build_manifest is True

    def test_uploaded_by_default(self, tmp_path):
        self._prepare(tmp_path)
        backend = _FakeBackend()
        result = ArtifactDeployer(self._config(), backend).deploy(str(tmp_path))

        assert "acme/board/rel/build-manifest.json" in backend.uploaded
        assert result.build_manifest_url == "fake://acme/board/rel/build-manifest.json"

    def test_found_in_build_subdirectory(self, tmp_path):
        self._prepare(tmp_path, write_manifest=False)
        nested = tmp_path / "build"
        nested.mkdir()
        (nested / "build-manifest.json").write_text("{}")
        backend = _FakeBackend()
        ArtifactDeployer(self._config(), backend).deploy(str(tmp_path))

        assert backend.uploaded["acme/board/rel/build-manifest.json"] == (
            nested / "build-manifest.json"
        )

    def test_missing_manifest_is_skipped(self, tmp_path):
        self._prepare(tmp_path, write_manifest=False)
        backend = _FakeBackend()
        result = ArtifactDeployer(self._config(), backend).deploy(str(tmp_path))

        assert result.build_manifest_url is None
        assert result.success_count == 1
        assert "acme/board/rel/build-manifest.json" not in backend.uploaded

    def test_disabled_by_config(self, tmp_path):
        self._prepare(tmp_path)
        backend = _FakeBackend()
        result = ArtifactDeployer(
            self._config(include_build_manifest=False), backend
        ).deploy(str(tmp_path))

        assert result.build_manifest_url is None
        assert "acme/board/rel/build-manifest.json" not in backend.uploaded

    def test_dry_run_does_not_upload(self, tmp_path):
        self._prepare(tmp_path)
        backend = _FakeBackend(dry_run=True)
        result = ArtifactDeployer(self._config(), backend).deploy(str(tmp_path))

        assert backend.uploaded == {}
        assert result.build_manifest_url == "dry-run:acme/board/rel/build-manifest.json"

    def test_uploaded_alongside_archive(self, tmp_path):
        from bsp.models import ArchiveConfig

        self._prepare(tmp_path)
        backend = _FakeBackend()
        cfg = self._config(archive=ArchiveConfig(name="bundle", format="tar.gz"))
        ArtifactDeployer(cfg, backend).deploy(str(tmp_path))

        assert "acme/board/rel/build-manifest.json" in backend.uploaded
        assert "acme/board/rel/bundle.tar.gz" in backend.uploaded

    def test_referenced_from_deploy_manifest(self, tmp_path):
        self._prepare(tmp_path)
        backend = _FakeBackend()
        deployer = ArtifactDeployer(self._config(include_manifest=True), backend)
        result = deployer.deploy(str(tmp_path))

        data = json.loads(deployer.generate_manifest(result))
        assert data["build_manifest"]["name"] == "build-manifest.json"
        assert data["build_manifest"]["remote_url"] == result.build_manifest_url

    def test_no_build_manifest_reference_when_absent(self, tmp_path):
        deployer = ArtifactDeployer(self._config(), _FakeBackend())
        data = json.loads(deployer.generate_manifest(DeployResult()))
        assert "build_manifest" not in data
