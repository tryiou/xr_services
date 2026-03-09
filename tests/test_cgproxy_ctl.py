"""

Tests for cgproxy_ctl.py - Command-line interface and orchestration.

Focused on:
- Argument parsing and command routing
- Target directory detection and validation
- Docker-compose interaction
- Health check logic
- Core command execution (install, uninstall, deploy, status, backup/restore)
- Error handling and user confirmations
"""

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cgproxy_ctl
from cgproxy_ctl import (
    Colors,
    _c,
    check_prereqs,
    cmd_backup,
    cmd_check,
    cmd_deploy,
    cmd_exec,
    cmd_help,
    cmd_install,
    cmd_list_backups,
    cmd_logs,
    cmd_rebuild,
    cmd_restart,
    cmd_restore,
    cmd_shell,
    cmd_status,
    cmd_undeploy,
    cmd_uninstall,
    compose,
    compose_rc,
    confirm,
    detect_target_dir,
    health_check,
    log_debug,
    log_error,
    log_info,
    log_warn,
    main,
    print_health,
    validate_target_dir,
)

# === Color and Logging Tests ===


def test_c_wraps_text_with_colors():
    """Test _c applies color codes and resets."""
    result = _c(Colors.RED, "error")
    assert Colors.RED in result
    assert "error" in result
    assert Colors.NC in result


@pytest.mark.parametrize(
    "func,color,expected_in_output",
    [
        (log_info, Colors.GREEN, "[INFO]"),
        (log_warn, Colors.YELLOW, "[WARN]"),
        (log_error, Colors.RED, "[ERROR]"),
    ],
)
def test_log_functions_output_prefixes(capsys, func, color, expected_in_output):  # noqa: ARG001
    """Test log functions print with correct colored prefixes."""
    func("test message")
    captured = capsys.readouterr()
    assert expected_in_output in captured.err
    assert "test message" in captured.err


def test_log_debug_respects_verbose_flag(capsys):
    """Test log_debug only prints when VERBOSE is True."""
    cgproxy_ctl.VERBOSE = False
    log_debug("silent")
    captured = capsys.readouterr()
    assert "[DEBUG]" not in captured.err

    cgproxy_ctl.VERBOSE = True
    log_debug("visible")
    captured = capsys.readouterr()
    assert "[DEBUG]" in captured.err and "visible" in captured.err


# === Target Directory Detection Tests ===


def test_detect_target_dir_from_env_var(monkeypatch, tmp_path):
    """Test detect_target_dir uses EXRPROXY_ENV when set and valid."""
    test_dir = str(tmp_path / "env")
    os.makedirs(test_dir)
    monkeypatch.setenv("EXRPROXY_ENV", test_dir)
    assert detect_target_dir() == test_dir


def test_detect_target_dir_ignores_invalid_env_var(monkeypatch, tmp_path):
    """Test detect_target_dir ignores EXRPROXY_ENV if not a directory."""
    monkeypatch.setenv("EXRPROXY_ENV", "/nonexistent/path")
    # Also ensure home dir doesn't exist to force None
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent_home")
    assert detect_target_dir() is None


def test_detect_target_dir_from_repo_layout(monkeypatch):
    """Test detect_target_dir returns parent when __file__ is in xr_services."""
    # Simulate being in repo structure: /path/xr_services/cgproxy_ctl.py
    fake_file = Path("/some/path/xr_services/cgproxy_ctl.py")
    monkeypatch.setattr(cgproxy_ctl, "REPO_DIR", fake_file.parent)
    assert detect_target_dir() == str(fake_file.parent.parent)


def test_detect_target_dir_from_home_dir(monkeypatch, tmp_path):
    """Test detect_target_dir returns ~/exrproxy-env if it exists."""
    home_env = tmp_path / "exrproxy-env"
    home_env.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert detect_target_dir() == str(home_env)


def test_detect_target_dir_returns_none_when_none_valid(monkeypatch):
    """Test detect_target_dir returns None when no candidates match."""
    monkeypatch.delenv("EXRPROXY_ENV", raising=False)
    # Set REPO_DIR to a path whose name is NOT "xr_services"
    monkeypatch.setattr(cgproxy_ctl, "REPO_DIR", Path("/not/other"))
    # Ensure home dir also doesn't exist
    monkeypatch.setattr(Path, "home", lambda: Path("/home/nonexistent"))
    assert detect_target_dir() is None


# === Target Directory Validation Tests ===


def test_validate_target_dir_missing_directory():
    """Test validate_target_dir fails for non-existent path."""
    assert validate_target_dir("/nonexistent") is False


def test_validate_target_dir_missing_docker_compose(tmp_path):
    """Test validate_target_dir fails without docker-compose.yml."""
    target = tmp_path / "target"
    target.mkdir()
    assert validate_target_dir(str(target)) is False


def test_validate_target_dir_valid_without_scripts(tmp_path):
    """Test validate_target_dir passes with just docker-compose.yml."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    assert validate_target_dir(str(target), require_scripts=False) is True


def test_validate_target_dir_requires_scripts_when_requested(tmp_path):
    """Test validate_target_dir checks for scripts when require_scripts=True."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    (target / "scripts").mkdir()
    (target / "scripts" / "start-xrproxy.sh").write_text("#!/bin/bash")
    (target / "scripts" / "start-snode.sh").write_text("#!/bin/bash")
    assert validate_target_dir(str(target), require_scripts=True) is True


def test_validate_target_dir_fails_missing_script(tmp_path):
    """Test validate_target_dir fails when required script is missing."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    (target / "scripts").mkdir()
    (target / "scripts" / "start-xrproxy.sh").write_text("#!/bin/bash")
    # Missing start-snode.sh
    assert validate_target_dir(str(target), require_scripts=True) is False


# === Prerequisites Check Tests ===


def test_check_prereqs_missing_docker(monkeypatch):
    """Test check_prereqs fails when docker is not in PATH."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None if cmd == "docker" else "/path")
    assert check_prereqs() is False


def test_check_prereqs_missing_docker_compose(monkeypatch):
    """Test check_prereqs fails when docker-compose is not in PATH."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None if cmd == "docker-compose" else "/path")
    assert check_prereqs() is False


def test_check_prereqs_missing_both(monkeypatch):
    """Test check_prereqs fails when both are missing."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert check_prereqs() is False


def test_check_prereqs_success(monkeypatch):
    """Test check_prereqs succeeds when both tools are present."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    assert check_prereqs() is True


# === Confirmation Tests ===


def test_confirm_force_skips_prompt():
    """Test confirm returns True immediately when force=True."""
    assert confirm(force=True) is True


def test_confirm_accepts_yes(monkeypatch):
    """Test confirm returns True for 'y' or 'yes'."""
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert confirm() is True
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert confirm() is True


def test_confirm_accepts_uppercase(monkeypatch):
    """Test confirm accepts uppercase responses."""
    monkeypatch.setattr("builtins.input", lambda _: "Y")
    assert confirm() is True
    monkeypatch.setattr("builtins.input", lambda _: "YES")
    assert confirm() is True


def test_confirm_rejects_no(monkeypatch):
    """Test confirm returns False for 'n' or 'no'."""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert confirm() is False
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert confirm() is False


def test_confirm_rejects_empty(monkeypatch):
    """Test confirm returns False for empty input."""
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert confirm() is False


# === Docker Compose Helper Tests ===


def test_run_success_captures_output(capsys):
    """Test _run captures and prints stdout/stderr on success."""
    result = cgproxy_ctl._run(["echo", "hello"], capture=True)
    assert result.returncode == 0
    captured = capsys.readouterr()
    # Output is printed to stderr in capture mode
    assert "hello" in captured.err


def test_run_failure_returns_error():
    """Test _run returns non-zero exit code and prints output."""
    result = cgproxy_ctl._run(["false"], capture=True)
    assert result.returncode != 0


def test_run_live_no_capture():
    """Test _run with capture=False streams output directly."""
    # live mode doesn't capture, output goes directly to sys.stderr
    result = cgproxy_ctl._run(["echo", "live"], capture=False)
    assert result.returncode == 0
    # Output should still appear (though capsys may not capture it in all scenarios)
    # In live mode, output goes to sys.stderr directly via subprocess.run
    # But since we're using a subprocess, the output may not be captured by capsys
    # So we just verify the command succeeded


def test_compose_missing_docker_compose(tmp_path, capsys):
    """Test compose returns False when docker-compose.yml is missing."""
    target = tmp_path / "target"
    target.mkdir()
    # No docker-compose.yml
    assert compose(str(target), "ps") is False
    captured = capsys.readouterr()
    assert "docker-compose.yml not found" in captured.err


def test_compose_command_failure(tmp_path, capsys):
    """Test compose returns False when docker-compose command fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    # Mock _run to fail
    with patch.object(cgproxy_ctl, "_run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="error")
        assert compose(str(target), "up") is False
        captured = capsys.readouterr()
        assert "docker-compose exited with code 1" in captured.err


def test_compose_success(tmp_path):
    """Test compose returns True on successful docker-compose execution."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "_run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert compose(str(target), "up", "-d") is True


def test_compose_rc_returns_exit_code(tmp_path):
    """Test compose_rc returns raw exit code for passthrough."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "_run") as mock_run:
        mock_run.return_value = MagicMock(returncode=42)
        rc = compose_rc(str(target), "logs")
        assert rc == 42


def test_compose_rc_missing_file_returns_1(tmp_path):
    """Test compose_rc returns 1 when docker-compose.yml missing."""
    target = tmp_path / "target"
    target.mkdir()
    rc = compose_rc(str(target), "ps")
    assert rc == 1


# === Health Check Tests ===


def test_health_check_missing_docker_compose(tmp_path):
    """Test health_check returns None when docker-compose.yml not found."""
    target = tmp_path / "target"
    target.mkdir()
    assert health_check(str(target)) is None


def test_health_check_container_not_running(tmp_path):
    """Test health_check returns None when container is not running."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "subprocess") as mock_subprocess:
        # Simulate container not running (empty stdout)
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="")
        assert health_check(str(target)) is None


def test_health_check_successful(tmp_path):
    """Test health_check returns response body when container is healthy."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    health_json = '{"status":"healthy","uptime":123}'
    with patch.object(cgproxy_ctl, "subprocess") as mock_subprocess:
        # First call: ps -q returns container ID (running)
        # Second call: exec health endpoint
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=0, stdout=health_json),
        ]
        result = health_check(str(target))
        assert result == health_json


def test_health_check_endpoint_error(tmp_path):
    """Test health_check returns None when health endpoint fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0, stdout="abc123\n"),
            MagicMock(returncode=1, stderr="connection error"),
        ]
        assert health_check(str(target)) is None


def test_print_health_formats_json(capsys, tmp_path):
    """Test print_health prints formatted JSON when health returns valid JSON."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    health_json = '{"status":"healthy"}'
    with patch.object(cgproxy_ctl, "health_check", return_value=health_json):
        print_health(str(target))
        captured = capsys.readouterr()
        assert '"status": "healthy"' in captured.out or '"status":"healthy"' in captured.out


def test_print_health_prints_raw_on_json_error(capsys, tmp_path):
    """Test print_health prints raw body when JSON parsing fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "health_check", return_value="not json"):
        print_health(str(target))
        captured = capsys.readouterr()
        assert "not json" in captured.out


def test_print_health_shows_warn_when_none(capsys, tmp_path):
    """Test print_health shows warning when health check returns None."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "health_check", return_value=None):
        print_health(str(target))
        captured = capsys.readouterr()
        assert "Health endpoint not responding" in captured.err or "[WARN]" in captured.err


# === Command Function Tests ===


def test_install_success_without_deploy(tmp_path, capsys):
    """Test cmd_install succeeds and does not deploy when --deploy not set."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with patch.object(cgproxy_ctl, "run_install", return_value=0) as mock_install:
        rc = cmd_install(str(target), dry_run=False, no_backup=False, deploy=False)
        assert rc == 0
        mock_install.assert_called_once_with(str(target), False, False)
        captured = capsys.readouterr()
        assert "Installation completed successfully" in captured.err


def test_install_success_with_deploy(tmp_path):
    """Test cmd_install calls cmd_deploy when --deploy is set."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with patch.object(cgproxy_ctl, "run_install", return_value=0), patch.object(cgproxy_ctl, "cmd_deploy", return_value=0) as mock_deploy:
        rc = cmd_install(str(target), dry_run=False, no_backup=False, deploy=True)
        assert rc == 0
        mock_deploy.assert_called_once_with(str(target))


def test_install_fails_when_run_install_errors(tmp_path):
    """Test cmd_install returns non-zero if run_install fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with patch.object(cgproxy_ctl, "run_install", return_value=1):
        rc = cmd_install(str(target), dry_run=False, no_backup=False, deploy=False)
        assert rc == 1


def test_install_deploy_failure_shows_warning(tmp_path, capsys):
    """Test cmd_install shows warning if deploy fails but install succeeded."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with patch.object(cgproxy_ctl, "run_install", return_value=0), patch.object(cgproxy_ctl, "cmd_deploy", return_value=1):
        rc = cmd_install(str(target), dry_run=False, no_backup=False, deploy=True)
        assert rc == 0
        captured = capsys.readouterr()
        assert "Deploy failed but configuration is installed" in captured.err


def test_uninstall_cancelled(monkeypatch, tmp_path):
    """Test cmd_uninstall returns 0 when user declines confirmation."""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = cmd_uninstall(str(tmp_path), dry_run=False, force=False)
    assert rc == 0


def test_uninstall_success(tmp_path):
    """Test cmd_uninstall calls run_uninstall and returns 0."""
    with patch.object(cgproxy_ctl, "run_uninstall", return_value=0) as mock_uninstall:
        rc = cmd_uninstall(str(tmp_path), dry_run=False, force=True)
        assert rc == 0
        mock_uninstall.assert_called_once_with(str(tmp_path), False, True)


def test_uninstall_failure(tmp_path):
    """Test cmd_uninstall returns non-zero if run_uninstall fails."""
    with patch.object(cgproxy_ctl, "run_uninstall", return_value=1):
        rc = cmd_uninstall(str(tmp_path), dry_run=False, force=True)
        assert rc == 1


def test_deploy_success(tmp_path):
    """Test cmd_deploy calls compose up -d and returns 0."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=True) as mock_compose:
        rc = cmd_deploy(str(target))
        assert rc == 0
        mock_compose.assert_called_once_with(str(target), "up", "-d", cgproxy_ctl.SERVICE_NAME)


def test_deploy_failure(tmp_path):
    """Test cmd_deploy returns 1 when compose fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=False):
        rc = cmd_deploy(str(target))
        assert rc == 1


def test_undeploy_cancelled(monkeypatch, tmp_path):
    """Test cmd_undeploy returns 0 when user declines."""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with patch.object(cgproxy_ctl, "compose", return_value=True):
        rc = cmd_undeploy(str(tmp_path), force=False)
        assert rc == 0


def test_undeploy_success(tmp_path):
    """Test cmd_undeploy calls compose down and returns 0."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=True) as mock_compose:
        rc = cmd_undeploy(str(target), force=True)
        assert rc == 0
        mock_compose.assert_called_once_with(str(target), "down", cgproxy_ctl.SERVICE_NAME)


def test_undeploy_failure(tmp_path):
    """Test cmd_undeploy returns 1 when compose down fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=False):
        rc = cmd_undeploy(str(tmp_path), force=True)
        assert rc == 1


def test_restart_success(tmp_path):
    """Test cmd_restart calls compose restart and returns 0."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=True) as mock_compose:
        rc = cmd_restart(str(target))
        assert rc == 0
        mock_compose.assert_called_once_with(str(target), "restart", cgproxy_ctl.SERVICE_NAME)


def test_restart_failure(tmp_path):
    """Test cmd_restart returns 1 when compose restart fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=False):
        rc = cmd_restart(str(target))
        assert rc == 1


def test_rebuild_success(tmp_path):
    """Test cmd_rebuild builds then redeploys and returns 0."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=True) as mock_compose:
        rc = cmd_rebuild(str(target))
        assert rc == 0
        # Should call build then up -d
        assert mock_compose.call_count == 2
        calls = [c[0] for c in mock_compose.call_args_list]
        assert (str(target), "build", cgproxy_ctl.SERVICE_NAME) in calls
        assert (str(target), "up", "-d", cgproxy_ctl.SERVICE_NAME) in calls


def test_rebuild_build_fails(tmp_path, capsys):
    """Test cmd_rebuild returns 1 if build fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose", return_value=False) as mock_compose:
        # Make first call (build) fail
        mock_compose.return_value = False
        rc = cmd_rebuild(str(target))
        assert rc == 1
        captured = capsys.readouterr()
        assert "Docker build failed" in captured.err


def test_status_shows_container_and_health(tmp_path, capsys):
    """Test cmd_status prints container ps and health check."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=0), patch.object(cgproxy_ctl, "print_health") as mock_health:
        rc = cmd_status(str(target))
        assert rc == 0
        captured = capsys.readouterr()
        assert "Container Status" in captured.out
        assert "Health Check" in captured.out
        mock_health.assert_called_once_with(str(target))


def test_status_when_not_running(tmp_path, capsys):
    """Test cmd_status shows warning when compose ps fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=1), patch.object(cgproxy_ctl, "print_health") as mock_health:
        rc = cmd_status(str(target))
        assert rc == 1
        captured = capsys.readouterr()
        assert "not running or not defined" in captured.err
        mock_health.assert_not_called()


def test_logs_without_follow(tmp_path):
    """Test cmd_logs calls compose with --tail=100 without -f."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=0) as mock_rc:
        rc = cmd_logs(str(target), follow=False)
        assert rc == 0
        mock_rc.assert_called_once()
        args = mock_rc.call_args[0]
        assert "logs" in args
        assert "--tail=100" in args
        assert "-f" not in args


def test_logs_with_follow(tmp_path):
    """Test cmd_logs calls compose with -f when follow=True."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=0) as mock_rc:
        rc = cmd_logs(str(target), follow=True)
        assert rc == 0
        mock_rc.assert_called_once()
        # live=True should be passed
        assert mock_rc.call_args[1].get("live") is True


def test_backup_success(tmp_path):
    """Test cmd_backup calls run_backup and returns 0."""
    with patch.object(cgproxy_ctl, "run_backup", return_value=0) as mock_backup:
        rc = cmd_backup(str(tmp_path), dry_run=False, tag=None)
        assert rc == 0
        mock_backup.assert_called_once_with(str(tmp_path), None, False)


def test_backup_failure(tmp_path):
    """Test cmd_backup returns non-zero if run_backup fails."""
    with patch.object(cgproxy_ctl, "run_backup", return_value=1):
        rc = cmd_backup(str(tmp_path), dry_run=False, tag=None)
        assert rc == 1


def test_restore_cancelled(monkeypatch, tmp_path):
    """Test cmd_restore returns 0 when user declines."""
    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = cmd_restore(str(tmp_path), dry_run=False, force=False, backup_id="12345")
    assert rc == 0


def test_restore_success(tmp_path):
    """Test cmd_restore calls run_restore and returns 0."""
    with patch.object(cgproxy_ctl, "run_restore", return_value=0) as mock_restore:
        rc = cmd_restore(str(tmp_path), dry_run=False, force=True, backup_id="12345")
        assert rc == 0
        mock_restore.assert_called_once_with(str(tmp_path), "12345", False)


def test_restore_failure(tmp_path):
    """Test cmd_restore returns non-zero if run_restore fails."""
    with patch.object(cgproxy_ctl, "run_restore", return_value=1):
        rc = cmd_restore(str(tmp_path), dry_run=False, force=True, backup_id="12345")
        assert rc == 1


def test_list_backups_success(tmp_path):
    """Test cmd_list_backups calls run_list_backups and returns its exit code."""
    with patch.object(cgproxy_ctl, "run_list_backups", return_value=0) as mock_list:
        rc = cmd_list_backups(str(tmp_path))
        assert rc == 0
        mock_list.assert_called_once_with(str(tmp_path))


def test_list_backups_failure(tmp_path):
    """Test cmd_list_backups propagates error code."""
    with patch.object(cgproxy_ctl, "run_list_backups", return_value=2):
        rc = cmd_list_backups(str(tmp_path))
        assert rc == 2


def test_check_all_good(tmp_path, capsys):
    """Test cmd_check when all prerequisites are met and service is running."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with (
        patch.object(cgproxy_ctl, "check_prereqs", return_value=True),
        patch.object(cgproxy_ctl, "compose_rc", return_value=0),
        patch.object(cgproxy_ctl, "health_check", return_value='{"status":"ok"}'),
    ):
        rc = cmd_check(str(target))
        assert rc == 0
        captured = capsys.readouterr()
        assert "No issues detected" in captured.err


def test_check_missing_prereqs(tmp_path, capsys):
    """Test cmd_check reports missing prerequisites."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")

    with patch.object(cgproxy_ctl, "check_prereqs", return_value=False):
        rc = cmd_check(str(target))
        assert rc == 1
        captured = capsys.readouterr()
        assert "issue(s) found" in captured.err


def test_check_missing_docker_compose_file(tmp_path, capsys):
    """Test cmd_check reports missing docker-compose.yml."""
    target = tmp_path / "target"
    target.mkdir()
    # No docker-compose.yml

    with patch.object(cgproxy_ctl, "check_prereqs", return_value=True):
        rc = cmd_check(str(target))
        assert rc == 1
        captured = capsys.readouterr()
        assert "docker-compose.yml not found" in captured.err


def test_check_service_not_running(tmp_path, capsys):
    """Test cmd_check notes when service is not running."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")

    with patch.object(cgproxy_ctl, "check_prereqs", return_value=True), patch.object(cgproxy_ctl, "compose_rc", return_value=1):
        rc = cmd_check(str(target))
        # Should still be 0 because service not running is just a warning, not an issue
        assert rc == 0
        captured = capsys.readouterr()
        assert "not running or not defined" in captured.err


def test_shell_success(tmp_path):
    """Test cmd_shell calls compose exec with shell."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=0) as mock_rc:
        rc = cmd_shell(str(target))
        # bash succeeds (rc != 126)
        assert rc == 0
        mock_rc.assert_called_once()
        args = mock_rc.call_args[0]
        assert "exec" in args
        assert cgproxy_ctl.SERVICE_NAME in args
        assert "bash" in args


def test_shell_fallback_to_sh(tmp_path):
    """Test cmd_shell tries sh if bash returns 126."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc") as mock_rc:
        # First call (bash) returns 126, second (sh) returns 0
        mock_rc.side_effect = [126, 0]
        rc = cmd_shell(str(target))
        assert rc == 0
        assert mock_rc.call_count == 2
        # Second call should use sh
        args2 = mock_rc.call_args_list[1][0]
        assert "sh" in args2


def test_shell_both_fail(tmp_path):
    """Test cmd_shell returns error if both shells fail with 126."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=126):
        rc = cmd_shell(str(target))
        assert rc == 1


def test_exec_with_command(tmp_path):
    """Test cmd_exec passes command to compose exec."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    with patch.object(cgproxy_ctl, "compose_rc", return_value=0) as mock_rc:
        rc = cmd_exec(str(target), ["ls", "-la"])
        assert rc == 0
        mock_rc.assert_called_once_with(str(target), "exec", cgproxy_ctl.SERVICE_NAME, "ls", "-la", live=True)


def test_exec_without_command(tmp_path, capsys):
    """Test cmd_exec returns error when no command provided."""
    rc = cmd_exec(str(tmp_path), [])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Usage: exec" in captured.err


def test_help_returns_zero_and_prints(capsys):
    """Test cmd_help prints help and returns 0."""
    rc = cmd_help()
    assert rc == 0
    captured = capsys.readouterr()
    assert "cgproxy_ctl.py" in captured.out
    assert "Usage:" in captured.out


# === Main Function Tests ===


def test_main_help_command(capsys):
    """Test main with no command or 'help' shows help."""
    with patch.object(cgproxy_ctl, "build_parser") as mock_parser:
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command=None, target_dir=None, verbose=False)
        mock_parser.return_value = parser
        rc = main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "cgproxy_ctl.py" in captured.out


def test_main_routes_commands_to_correct_functions(tmp_path):
    """Test main dispatches each command to the right function."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    # Commands to test: (command, function_name)
    commands_to_test = [
        ("install", "cmd_install"),
        ("uninstall", "cmd_uninstall"),
        ("deploy", "cmd_deploy"),
        ("undeploy", "cmd_undeploy"),
        ("restart", "cmd_restart"),
        ("rebuild", "cmd_rebuild"),
        ("status", "cmd_status"),
        ("logs", "cmd_logs"),
        ("backup", "cmd_backup"),
        ("restore", "cmd_restore"),
        ("list-backups", "cmd_list_backups"),
        ("check", "cmd_check"),
        ("shell", "cmd_shell"),
        ("exec", "cmd_exec"),
    ]

    for command, func_name in commands_to_test:
        # Create fresh mocks for each iteration
        with (
            patch.object(cgproxy_ctl, "build_parser") as mock_parser,
            patch.object(cgproxy_ctl, "detect_target_dir", return_value=str(target)),
            patch.object(cgproxy_ctl, "validate_target_dir", return_value=True),
            patch.object(cgproxy_ctl, "cmd_help"),
            patch.object(cgproxy_ctl, func_name) as mock_func,
        ):
            parser = MagicMock()
            # Set up command-specific args
            parse_kwargs = {"command": command, "target_dir": None, "verbose": False}
            if command == "install":
                parse_kwargs.update({"dry_run": False, "no_backup": False, "deploy": False})
            elif command == "uninstall":
                parse_kwargs.update({"dry_run": False, "force": False})
            elif command == "undeploy":
                parse_kwargs.update({"force": False})
            elif command == "logs":
                parse_kwargs.update({"follow": False})
            elif command == "backup":
                parse_kwargs.update({"dry_run": False, "tag": None})
            elif command == "restore":
                parse_kwargs.update({"dry_run": False, "force": False, "backup_id": "dummy"})
            elif command == "exec":
                parse_kwargs.update({"exec_cmd": ["echo"]})

            parser.parse_args.return_value = MagicMock(**parse_kwargs)
            mock_parser.return_value = parser

            # Mock command function to return 0
            mock_func.return_value = 0

            rc = main()
            # The expected function should have been called once
            mock_func.assert_called_once()
            # main should return the function's return value (we set return_value=0)
            assert rc == 0


def test_main_auto_detects_target_dir(tmp_path):
    """Test main uses detect_target_dir when --target-dir not provided."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    scripts = target / "scripts"
    scripts.mkdir()
    (scripts / "start-xrproxy.sh").write_text("#!/bin/bash")
    (scripts / "start-snode.sh").write_text("#!/bin/bash")

    with (
        patch.object(cgproxy_ctl, "build_parser") as mock_parser,
        patch.object(cgproxy_ctl, "detect_target_dir", return_value=str(target)) as mock_detect,
        patch.object(cgproxy_ctl, "validate_target_dir", return_value=True),
        patch.object(cgproxy_ctl, "cmd_help"),
    ):
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command="status", target_dir=None, verbose=False)
        mock_parser.return_value = parser

        _ = cgproxy_ctl.main()
        mock_detect.assert_called_once()


def test_main_uses_provided_target_dir(tmp_path):
    """Test main uses expanded absolute path from --target-dir."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")

    with (
        patch.object(cgproxy_ctl, "build_parser") as mock_parser,
        patch.object(cgproxy_ctl, "validate_target_dir", return_value=True),
        patch.object(cgproxy_ctl, "cmd_help"),
    ):
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command="status", target_dir="~/target", verbose=False)
        mock_parser.return_value = parser

        # We'll capture the target_dir passed to validate and command
        captured_targets = []

        def capture_validate(path, require_scripts=False):
            _ = require_scripts  # Mark as intentionally unused
            captured_targets.append(path)
            return True

        with patch.object(cgproxy_ctl, "validate_target_dir", side_effect=capture_validate):
            _ = cgproxy_ctl.main()

        # Should be expanded to absolute path with ~ resolved
        assert len(captured_targets) >= 1
        # All captured paths should be absolute and not contain ~
        for p in captured_targets:
            assert not p.startswith("~")
            assert os.path.isabs(p)


def test_main_verbose_flag_sets_global():
    """Test main sets VERBOSE global when --verbose is passed."""
    with (
        patch.object(cgproxy_ctl, "build_parser") as mock_parser,
        patch.object(cgproxy_ctl, "detect_target_dir", return_value=None),
        patch.object(cgproxy_ctl, "cmd_help"),
    ):
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command=None, target_dir=None, verbose=True)
        mock_parser.return_value = parser

        _ = cgproxy_ctl.main()
        # Should call help; VERBOSE should be True inside
        assert cgproxy_ctl.VERBOSE is True


def test_main_validation_fails_returns_1(tmp_path):
    """Test main returns 1 when validation fails."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")

    with (
        patch.object(cgproxy_ctl, "build_parser") as mock_parser,
        patch.object(cgproxy_ctl, "detect_target_dir", return_value=str(target)),
        patch.object(cgproxy_ctl, "validate_target_dir", return_value=False),
    ):
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command="install", target_dir=None, verbose=False, dry_run=False, no_backup=False, deploy=False)
        mock_parser.return_value = parser

        rc = cgproxy_ctl.main()
        assert rc == 1


def test_main_unknown_command(capsys):
    """Test main returns 1 and logs error for unknown command."""
    with patch.object(cgproxy_ctl, "build_parser") as mock_parser:
        parser = MagicMock()
        parser.parse_args.return_value = MagicMock(command="unknown_command", target_dir=None, verbose=False)
        mock_parser.return_value = parser

        rc = cgproxy_ctl.main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "Unknown command" in captured.err
