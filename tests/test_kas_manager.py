"""
Tests for KasManager KAS/Yocto build orchestration.
"""

import os
from unittest.mock import patch

from bsp import KasManager


class TestKasManager:
    def test_init_basic(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build")
        )
        assert manager.kas_files == [str(kas_config_file)]
        assert manager.use_container is False

    def test_init_requires_non_empty_kas_files(self, tmp_dir):
        import pytest
        with pytest.raises(SystemExit):
            KasManager(kas_files=[], build_dir=str(tmp_dir / "build"))

    def test_init_requires_list_kas_files(self, tmp_dir):
        import pytest
        with pytest.raises(SystemExit):
            KasManager(kas_files="not-a-list", build_dir=str(tmp_dir / "build"))

    def test_get_kas_command_native(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False
        )
        assert manager._get_kas_command() == ["kas"]

    def test_get_kas_command_container(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True
        )
        assert manager._get_kas_command() == ["kas-container"]

    def test_get_kas_command_container_privileged(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_privileged=True
        )
        cmd = manager._get_kas_command()
        assert cmd == ["kas-container", "--isar"]

    def test_get_kas_command_privileged_not_applied_without_container(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False,
            container_privileged=True
        )
        assert manager._get_kas_command() == ["kas"]

    def test_resolve_kas_file_absolute(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build")
        )
        resolved = manager._resolve_kas_file(str(kas_config_file))
        assert resolved == str(kas_config_file)

    def test_resolve_kas_file_not_found_exits(self, tmp_dir):
        import pytest
        manager = KasManager(
            kas_files=[str(tmp_dir / "nonexistent.yml")],
            build_dir=str(tmp_dir / "build")
        )
        with pytest.raises(SystemExit):
            manager._resolve_kas_file("totally_missing_file.yml")

    def test_get_kas_files_string(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build")
        )
        result = manager._get_kas_files_string()
        assert str(kas_config_file) in result

    def test_get_kas_files_string_multiple(self, tmp_dir):
        file1 = tmp_dir / "file1.yml"
        file2 = tmp_dir / "file2.yml"
        file1.write_text("header:\n  version: 14\n")
        file2.write_text("header:\n  version: 14\n")
        manager = KasManager(
            kas_files=[str(file1), str(file2)],
            build_dir=str(tmp_dir / "build")
        )
        result = manager._get_kas_files_string()
        assert ":" in result

    def test_find_includes_in_yaml_top_level(self):
        content = {"includes": ["file1.yml", "file2.yml"]}
        manager = KasManager.__new__(KasManager)
        result = manager._find_includes_in_yaml(content)
        assert result == ["file1.yml", "file2.yml"]

    def test_find_includes_in_yaml_header(self):
        content = {"header": {"includes": ["file1.yml"]}}
        manager = KasManager.__new__(KasManager)
        result = manager._find_includes_in_yaml(content)
        assert result == ["file1.yml"]

    def test_find_includes_in_yaml_empty(self):
        content = {"machine": "qemuarm64"}
        manager = KasManager.__new__(KasManager)
        result = manager._find_includes_in_yaml(content)
        assert result == []

    def test_find_includes_both_sources(self):
        content = {
            "includes": ["top.yml"],
            "header": {"includes": ["header.yml"]}
        }
        manager = KasManager.__new__(KasManager)
        result = manager._find_includes_in_yaml(content)
        assert "top.yml" in result
        assert "header.yml" in result

    def test_validate_kas_files_success(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build")
        )
        assert manager.validate_kas_files(check_includes=False) is True

    def test_validate_kas_files_with_includes(self, kas_config_with_includes):
        base_path, include_path = kas_config_with_includes
        manager = KasManager(
            kas_files=[str(base_path)],
            build_dir=str(base_path.parent / "build")
        )
        assert manager.validate_kas_files(check_includes=True) is True

    def test_parse_yaml_file_with_cache(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build")
        )
        result1 = manager._parse_yaml_file(str(kas_config_file))
        result2 = manager._parse_yaml_file(str(kas_config_file))
        assert result1 == result2
        assert str(kas_config_file) in manager._yaml_cache

    def test_environment_variables_in_kas_env(self, kas_config_file):
        with patch.dict(os.environ, {"DL_DIR": "/custom/downloads"}):
            manager = KasManager(
                kas_files=[str(kas_config_file)],
                build_dir=str(kas_config_file.parent / "build"),
                download_dir="/custom/downloads"
            )
            env = manager._get_environment_with_container_vars()
            assert env.get("DL_DIR") == "/custom/downloads"

    def test_container_env_vars_set_when_using_container(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_engine="docker",
            container_image="custom-image:latest"
        )
        env = manager._get_environment_with_container_vars()
        assert env.get("KAS_CONTAINER_ENGINE") == "docker"
        assert env.get("KAS_CONTAINER_IMAGE") == "custom-image:latest"

    def test_check_kas_available_when_installed(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False
        )
        # kas should be available in the test environment (installed via pip)
        result = manager.check_kas_available()
        assert isinstance(result, bool)

    def test_container_runtime_args_stored(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_runtime_args="-p 2222:2222 --cap-add=NET_ADMIN"
        )
        assert manager.container_runtime_args == "-p 2222:2222 --cap-add=NET_ADMIN"

    def test_container_runtime_args_default_none(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
        )
        assert manager.container_runtime_args is None

    def test_container_runtime_args_set_in_env_when_using_container(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_runtime_args="-p 2222:2222 --device=/dev/net/tun"
        )
        env = manager._get_environment_with_container_vars()
        assert env.get("KAS_RUNTIME_ARGS") == "-p 2222:2222 --device=/dev/net/tun"

    def test_container_runtime_args_not_set_when_none(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
        )
        env = manager._get_environment_with_container_vars()
        assert "KAS_RUNTIME_ARGS" not in env

    def test_container_privileged_stored(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            container_privileged=True
        )
        assert manager.container_privileged is True

    def test_container_volumes_default_empty(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
        )
        assert manager.container_volumes == []

    def test_container_volumes_stored(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            container_volumes=vols,
        )
        assert manager.container_volumes == vols

    def test_container_volumes_appended_to_kas_container_args(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        env = manager._get_environment_with_container_vars()
        assert env.get("KAS_RUNTIME_ARGS") == "-v /host/data:/data"

    def test_container_volumes_read_only_flag(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="/host/ro", container="/ro", read_only=True)]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        env = manager._get_environment_with_container_vars()
        assert env.get("KAS_RUNTIME_ARGS") == "-v /host/ro:/ro:ro"

    def test_container_volumes_combined_with_runtime_args(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_runtime_args="-p 2222:2222",
            container_volumes=vols,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        assert "-p 2222:2222" in kas_args
        assert "-v /host/data:/data" in kas_args

    def test_container_volumes_multiple(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [
            DockerVolume(host="/host/a", container="/a"),
            DockerVolume(host="/host/b", container="/b", read_only=True),
        ]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        assert "-v /host/a:/a" in kas_args
        assert "-v /host/b:/b:ro" in kas_args

    def test_container_volumes_env_expansion(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="$ENV{TEST_HOST_DIR}", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        with patch.dict(os.environ, {"TEST_HOST_DIR": "/expanded/path"}):
            env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        assert "-v /expanded/path:/data" in kas_args

    def test_container_volumes_not_set_without_container_mode(self, kas_config_file):
        from bsp.models import DockerVolume
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False,
            container_volumes=vols,
        )
        env = manager._get_environment_with_container_vars()
        assert "KAS_RUNTIME_ARGS" not in env

    # ------------------------------------------------------------------
    # env_manager integration with container args
    # ------------------------------------------------------------------

    def test_env_manager_kas_container_args_merged_with_volumes(self, kas_config_file):
        """KAS_RUNTIME_ARGS set via env_manager is preserved and volumes are appended."""
        from bsp.models import DockerVolume, EnvironmentVariable
        from bsp.environment import EnvironmentManager
        vols = [DockerVolume(host="/host/data", container="/data")]
        env_mgr = EnvironmentManager([
            EnvironmentVariable(name="KAS_RUNTIME_ARGS", value="--extra-flag"),
        ])
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
            env_manager=env_mgr,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        assert "--extra-flag" in kas_args
        assert "-v /host/data:/data" in kas_args

    def test_env_manager_vars_forwarded_as_e_flags_in_container_mode(self, kas_config_file):
        """Registry env vars are forwarded as -e flags inside KAS_RUNTIME_ARGS."""
        from bsp.models import EnvironmentVariable
        from bsp.environment import EnvironmentManager
        env_mgr = EnvironmentManager([
            EnvironmentVariable(name="MY_CUSTOM_VAR", value="my_value"),
            EnvironmentVariable(name="ANOTHER_VAR", value="another"),
        ])
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            env_manager=env_mgr,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        assert "-e MY_CUSTOM_VAR=my_value" in kas_args
        assert "-e ANOTHER_VAR=another" in kas_args

    def test_env_manager_vars_not_forwarded_without_container_mode(self, kas_config_file):
        """Registry env vars are NOT added as -e flags when not using container mode."""
        from bsp.models import EnvironmentVariable
        from bsp.environment import EnvironmentManager
        env_mgr = EnvironmentManager([
            EnvironmentVariable(name="MY_CUSTOM_VAR", value="my_value"),
        ])
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False,
            env_manager=env_mgr,
        )
        env = manager._get_environment_with_container_vars()
        assert "KAS_RUNTIME_ARGS" not in env
        # The var is still set in the host environment
        assert env.get("MY_CUSTOM_VAR") == "my_value"

    def test_env_manager_kas_container_args_not_overwritten_by_env_manager(self, kas_config_file):
        """env_manager KAS_RUNTIME_ARGS cannot overwrite volumes set via container_volumes."""
        from bsp.models import DockerVolume, EnvironmentVariable
        from bsp.environment import EnvironmentManager
        vols = [DockerVolume(host="/host/src", container="/src")]
        env_mgr = EnvironmentManager([
            EnvironmentVariable(name="KAS_RUNTIME_ARGS", value="--net=host"),
        ])
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
            env_manager=env_mgr,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = env.get("KAS_RUNTIME_ARGS", "")
        # Both the env_manager value AND the volume must survive.
        assert "--net=host" in kas_args
        assert "-v /host/src:/src" in kas_args

    # ------------------------------------------------------------------
    # KAS_RUNTIME_ARGS debug logging in _run_kas_command
    # ------------------------------------------------------------------

    def test_kas_container_args_logged_at_debug_in_container_mode(self, kas_config_file, caplog):
        """KAS_RUNTIME_ARGS is logged at DEBUG level when use_container=True."""
        import logging
        from bsp.models import DockerVolume
        from unittest.mock import patch as mock_patch
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        with caplog.at_level(logging.DEBUG, logger="root"):
            with mock_patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                try:
                    manager._run_kas_command(["build", str(kas_config_file)])
                except SystemExit:
                    pass
        assert any("KAS_RUNTIME_ARGS" in record.message for record in caplog.records)

    def test_kas_container_args_not_logged_in_native_mode(self, kas_config_file, caplog):
        """KAS_RUNTIME_ARGS is NOT logged when use_container=False."""
        import logging
        from unittest.mock import patch as mock_patch
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False,
        )
        with caplog.at_level(logging.DEBUG, logger="root"):
            with mock_patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                try:
                    manager._run_kas_command(["build", str(kas_config_file)])
                except SystemExit:
                    pass
        assert not any("KAS_RUNTIME_ARGS" in record.message for record in caplog.records)
