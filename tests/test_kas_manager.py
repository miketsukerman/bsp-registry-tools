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

    def test_get_kas_command_container_verbose(self, kas_config_file):
        """verbose=True adds -l debug to kas-container command."""
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            verbose=True,
        )
        assert manager._get_kas_command() == ["kas-container", "-l", "debug"]

    def test_get_kas_command_container_verbose_with_privileged(self, kas_config_file):
        """verbose=True with privileged adds --isar and -l debug."""
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_privileged=True,
            verbose=True,
        )
        assert manager._get_kas_command() == ["kas-container", "--isar", "-l", "debug"]

    def test_get_kas_command_native_verbose_no_debug_flag(self, kas_config_file):
        """-l debug is NOT added for native (non-container) builds."""
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=False,
            verbose=True,
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
        assert manager._build_runtime_args_str(env) == "-p 2222:2222 --device=/dev/net/tun"

    def test_container_runtime_args_not_set_when_none(self, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
        )
        env = manager._get_environment_with_container_vars()
        assert manager._build_runtime_args_str(env) is None

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
        assert manager._build_runtime_args_str(env) == "-v /host/data:/data"

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
        assert manager._build_runtime_args_str(env) == "-v /host/ro:/ro:ro"

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
        kas_args = manager._build_runtime_args_str(env)
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
        kas_args = manager._build_runtime_args_str(env)
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
            kas_args = manager._build_runtime_args_str(env)
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
        assert manager._build_runtime_args_str(env) is None

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
        kas_args = manager._build_runtime_args_str(env)
        assert "--extra-flag" in kas_args
        assert "-v /host/data:/data" in kas_args

    def test_env_manager_vars_in_process_env_in_container_mode(self, kas_config_file):
        """Registry env vars are set in the process environment, not as -e flags."""
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
        # Vars are in the process environment
        assert env.get("MY_CUSTOM_VAR") == "my_value"
        assert env.get("ANOTHER_VAR") == "another"
        # Vars are NOT duplicated as -e flags in --runtime-args
        kas_args = manager._build_runtime_args_str(env) or ""
        assert "-e MY_CUSTOM_VAR" not in kas_args
        assert "-e ANOTHER_VAR" not in kas_args

    def test_env_manager_vars_in_process_env_without_container_mode(self, kas_config_file):
        """Registry env vars are set in the process environment in both container and native mode."""
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
        assert env.get("MY_CUSTOM_VAR") == "my_value"
        assert manager._build_runtime_args_str(env) is None

    def test_no_e_flags_in_runtime_args_for_any_env_var(self, kas_config_file):
        """No env var — including DL_DIR, SSTATE_DIR, GITCONFIG_FILE — appears as -e flag."""
        from bsp.models import EnvironmentVariable
        from bsp.environment import EnvironmentManager
        env_mgr = EnvironmentManager([
            EnvironmentVariable(name="DL_DIR", value="/downloads"),
            EnvironmentVariable(name="SSTATE_DIR", value="/sstate"),
            EnvironmentVariable(name="GITCONFIG_FILE", value="/home/user/.gitconfig"),
            EnvironmentVariable(name="MY_CUSTOM_VAR", value="value"),
        ])
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            env_manager=env_mgr,
        )
        env = manager._get_environment_with_container_vars()
        kas_args = manager._build_runtime_args_str(env) or ""
        assert " -e " not in f" {kas_args} "

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
        kas_args = manager._build_runtime_args_str(env)
        # Both the env_manager value AND the volume must survive.
        assert "--net=host" in kas_args
        assert "-v /host/src:/src" in kas_args

    # ------------------------------------------------------------------
    # --runtime-args in _run_kas_command
    # ------------------------------------------------------------------

    def test_kas_container_args_logged_at_debug_in_container_mode(self, kas_config_file, caplog):
        """--runtime-args is logged at DEBUG level when use_container=True."""
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
        assert any("--runtime-args" in record.message for record in caplog.records)

    def test_kas_container_args_not_logged_in_native_mode(self, kas_config_file, caplog):
        """--runtime-args is NOT logged when use_container=False."""
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
        assert not any("--runtime-args" in record.message for record in caplog.records)

    def test_run_kas_command_passes_runtime_args_in_cmd(self, kas_config_file):
        """_run_kas_command includes --runtime-args in the subprocess call."""
        from bsp.models import DockerVolume
        from unittest.mock import patch as mock_patch, call
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        with mock_patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            try:
                manager._run_kas_command(["build", str(kas_config_file)])
            except SystemExit:
                pass
        called_cmd = mock_run.call_args[0][0]
        assert "--runtime-args" in called_cmd
        rt_idx = called_cmd.index("--runtime-args")
        assert "-v /host/data:/data" in called_cmd[rt_idx + 1]

    def test_run_kas_command_no_runtime_args_when_nothing_to_pass(self, kas_config_file):
        """_run_kas_command does NOT include --runtime-args when nothing is configured."""
        from unittest.mock import patch as mock_patch
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
        )
        with mock_patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            try:
                manager._run_kas_command(["build", str(kas_config_file)])
            except SystemExit:
                pass
        called_cmd = mock_run.call_args[0][0]
        assert "--runtime-args" not in called_cmd

    def test_run_kas_command_kas_runtime_args_not_in_env(self, kas_config_file):
        """KAS_RUNTIME_ARGS is removed from the environment before the subprocess call."""
        from bsp.models import DockerVolume
        from unittest.mock import patch as mock_patch
        vols = [DockerVolume(host="/host/data", container="/data")]
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(kas_config_file.parent / "build"),
            use_container=True,
            container_volumes=vols,
        )
        with mock_patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            try:
                manager._run_kas_command(["build", str(kas_config_file)])
            except SystemExit:
                pass
        # env is passed as a keyword arg
        called_env = mock_run.call_args[1]["env"] if mock_run.call_args[1] else mock_run.call_args.kwargs["env"]
        assert "KAS_RUNTIME_ARGS" not in called_env
