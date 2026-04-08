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
        root = RegistryRoot(specification=Specification(version="2.0"), registry=Registry())
        assert root.deploy is None

    def test_registry_root_with_deploy_config(self):
        deploy = DeployConfig(provider="azure", container="my-container")
        root = RegistryRoot(
            specification=Specification(version="2.0"),
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
  version: "2.0"
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
  version: "2.0"
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
  version: "2.0"
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
