#!/usr/bin/env python3
"""cgproxy_ctl.py — Unified lifecycle management for Coingecko Proxy."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Configure root logger to capture installer module logs
# Installer uses standard logging; we redirect to stderr
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stderr)])

# Import installer functions directly (no subprocess)
try:
    from install_cg_proxy_xrs import (
        run_backup,
        run_install,
        run_list_backups,
        run_restore,
        run_uninstall,
    )

    INSTALLER_IMPORTED = True
except ImportError as e:
    # This should not happen in normal deployment, but we fail fast
    print(f"ERROR: Cannot import installer functions: {e}", file=sys.stderr)
    print("Make sure install_cg_proxy_xrs.py is in the same directory.", file=sys.stderr)
    sys.exit(1)

# === Version check ===
MIN_PYTHON = (3, 6)
if sys.version_info < MIN_PYTHON:
    print(
        f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required (found {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(1)

# === Constants ===
REPO_DIR = Path(__file__).parent.resolve()
SERVICE_NAME = "xr_service_cg_proxy"
SERVICE_PORT = "8080"

# === Module-level verbose flag (set once in main, read everywhere) ===
VERBOSE = False


# === Colors ===
class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{Colors.NC}"


def log_info(msg: str) -> None:
    print(_c(Colors.GREEN, "[INFO]"), msg, file=sys.stderr)


def log_warn(msg: str) -> None:
    print(_c(Colors.YELLOW, "[WARN]"), msg, file=sys.stderr)


def log_error(msg: str) -> None:
    print(_c(Colors.RED, "[ERROR]"), msg, file=sys.stderr)


def log_debug(msg: str) -> None:
    if VERBOSE:
        print(_c(Colors.BLUE, "[DEBUG]"), msg, file=sys.stderr)


# === Core Helpers ===


def detect_target_dir() -> str | None:
    """Auto-detect the exrproxy-env directory via env var, repo layout, or default."""
    exrproxy_env = os.environ.get("EXRPROXY_ENV")
    if exrproxy_env and os.path.isdir(exrproxy_env):
        return exrproxy_env
    if REPO_DIR.name == "xr_services":
        return str(REPO_DIR.parent)
    home_env = Path.home() / "exrproxy-env"
    if home_env.is_dir():
        return str(home_env)
    return None


def validate_target_dir(target_dir: str, require_scripts: bool = False) -> bool:
    """
    Validate target_dir as a usable exrproxy-env.
    require_scripts=True also checks for scripts/start-*.sh files.
    """
    if not os.path.isdir(target_dir):
        log_error(f"Target directory does not exist: {target_dir}")
        return False
    if not (Path(target_dir) / "docker-compose.yml").is_file():
        log_error("docker-compose.yml not found — not a valid exrproxy-env.")
        return False
    if require_scripts:
        missing = [rel for rel in ("scripts/start-xrproxy.sh", "scripts/start-snode.sh") if not (Path(target_dir) / rel).is_file()]
        if missing:
            for f in missing:
                log_error(f"Missing essential file: {f}")
            return False
    return True


def check_prereqs() -> bool:
    """Verify docker and docker-compose are on PATH."""
    missing = [t for t in ("docker", "docker-compose") if not shutil.which(t)]
    if missing:
        log_error(f"Missing prerequisites: {', '.join(missing)}")
        return False
    return True


def confirm(force: bool = False) -> bool:
    """Prompt user for confirmation; skip if force=True."""
    if force:
        return True
    return input("Continue? [y/N] ").strip().lower() in ("y", "yes")


# === Subprocess Wrappers ===


def _run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally capturing output."""
    log_debug(f"Run: {' '.join(cmd)}")
    if capture:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        return result
    # Live output (for logs --follow, shell, exec)
    return subprocess.run(cmd, text=True)


def compose(target_dir: str, *args: str, live: bool = False) -> bool:
    """
    Run docker-compose against target_dir's compose file.
    Returns True on success, False on failure.
    live=True streams output directly (for interactive/tail commands).
    """
    compose_file = Path(target_dir) / "docker-compose.yml"
    if not compose_file.exists():
        log_error(f"docker-compose.yml not found in {target_dir}")
        return False
    cmd = ["docker-compose", "-f", str(compose_file), *args]
    result = _run(cmd, capture=not live)
    if result.returncode != 0:
        log_error(f"docker-compose exited with code {result.returncode}")
        return False
    return True


def compose_rc(target_dir: str, *args: str, live: bool = False) -> int:
    """Like compose() but returns the raw exit code (for pass-through commands)."""
    compose_file = Path(target_dir) / "docker-compose.yml"
    if not compose_file.exists():
        log_error(f"docker-compose.yml not found in {target_dir}")
        return 1
    cmd = ["docker-compose", "-f", str(compose_file), *args]
    return _run(cmd, capture=not live).returncode


# === Service Utilities ===


def health_check(target_dir: str) -> str | None:
    """Check health by executing Python inside the container; return body string or None on failure."""
    compose_file = Path(target_dir) / "docker-compose.yml"
    if not compose_file.exists():
        log_debug(f"docker-compose.yml not found in {target_dir}")
        return None

    # Check if container is running first
    result = subprocess.run(
        ["docker-compose", "-f", str(compose_file), "ps", "-q", SERVICE_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        log_debug("Service is not running")
        return None

    # Execute Python inside the container to fetch health endpoint
    python_cmd = "import urllib.request, sys; sys.stdout.write(urllib.request.urlopen('http://localhost:8080/health', timeout=5).read().decode())"
    result = subprocess.run(
        ["docker-compose", "-f", str(compose_file), "exec", "-T", SERVICE_NAME, "python", "-c", python_cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    else:
        log_debug(f"Health check failed: {result.stderr.strip() if result.stderr else 'empty response'}")
        return None


def print_health(target_dir: str) -> None:
    """Print health check result, formatted as JSON if possible."""
    body = health_check(target_dir)
    if body:
        try:
            print(json.dumps(json.loads(body), indent=2))
        except json.JSONDecodeError:
            print(body)
    else:
        log_warn("Health endpoint not responding")


# === Command Implementations ===


def cmd_install(target_dir: str, dry_run: bool, no_backup: bool, deploy: bool) -> int:
    log_info(f"Installing Coingecko Proxy → {target_dir}")
    rc = run_install(target_dir, dry_run, no_backup)
    if rc != 0:
        log_error("Installation failed.")
        return rc
    log_info("Installation completed successfully.")
    if deploy:
        log_info("Deploying service container...")
        rc = cmd_deploy(target_dir)
        if rc != 0:
            log_warn("Deploy failed but configuration is installed.")
        else:
            log_info("Service is starting. Use 'cgproxy_ctl.py status' to check.")
    return 0


def cmd_uninstall(target_dir: str, dry_run: bool, force: bool) -> int:
    log_warn("This will: restore modified files from backup, remove plugin files, stop container.")
    if not confirm(force):
        log_info("Uninstall cancelled.")
        return 0

    rc = run_uninstall(target_dir, dry_run, force)
    if rc != 0:
        log_error("Uninstall failed.")
        return rc

    log_info("Uninstall completed successfully.")
    return 0


def cmd_deploy(target_dir: str) -> int:
    log_info("Starting Docker container...")
    if not compose(target_dir, "up", "-d", SERVICE_NAME):
        log_error("Failed to start container.")
        return 1
    log_info(f"Service running on internal port {SERVICE_PORT} (Docker network only)")
    log_info("Use 'cgproxy_ctl.py status' to check health")
    return 0


def cmd_undeploy(target_dir: str, force: bool) -> int:
    log_warn("This will stop and remove the Docker container.")
    if not confirm(force):
        log_info("Undeploy cancelled.")
        return 0
    if not compose(target_dir, "down", SERVICE_NAME):
        log_error("Failed to stop container.")
        return 1
    log_info("Container stopped and removed.")
    return 0


def cmd_restart(target_dir: str) -> int:
    log_info("Restarting service...")
    if not compose(target_dir, "restart", SERVICE_NAME):
        log_error("Failed to restart container.")
        return 1
    log_info("Service restarted.")
    return 0


def cmd_rebuild(target_dir: str) -> int:
    log_info("Rebuilding Docker image...")
    if not compose(target_dir, "build", SERVICE_NAME):
        log_error("Docker build failed.")
        return 1
    log_info("Deploying fresh container...")
    if not compose(target_dir, "up", "-d", SERVICE_NAME):
        log_error("Failed to deploy container after rebuild.")
        return 1
    log_info("Rebuild completed successfully.")
    return 0


def cmd_status(target_dir: str) -> int:
    print("=== Container Status ===")
    rc = compose_rc(target_dir, "ps", SERVICE_NAME)
    if rc != 0:
        log_warn("Service is not running or not defined in docker-compose.yml")
        return 1
    print("\n=== Health Check ===")
    print_health(target_dir)
    return 0


def cmd_logs(target_dir: str, follow: bool) -> int:
    args = ["logs", "--tail=100"]
    if follow:
        args.append("-f")
    args.append(SERVICE_NAME)
    return compose_rc(target_dir, *args, live=True)


def cmd_backup(target_dir: str, dry_run: bool, tag: str | None) -> int:
    log_info("Creating backup...")
    rc = run_backup(target_dir, tag, dry_run)
    if rc != 0:
        log_error("Backup failed.")
        return rc
    log_info("Backup created successfully.")
    return 0


def cmd_restore(target_dir: str, dry_run: bool, force: bool, backup_id: str) -> int:
    log_warn(f"This will restore files from backup '{backup_id}'. Changes after that backup will be lost.")
    if not confirm(force):
        log_info("Restore cancelled.")
        return 0

    rc = run_restore(target_dir, backup_id, dry_run)
    if rc != 0:
        log_error("Restore failed.")
        return rc
    log_info("Restore completed successfully.")
    return 0


def cmd_list_backups(target_dir: str) -> int:
    return run_list_backups(target_dir)


def cmd_check(target_dir: str) -> int:
    issues = 0

    print("=== Prerequisites ===")
    if check_prereqs():
        print("✓ docker and docker-compose found")
    else:
        issues += 1

    print("\n=== Target Directory ===")
    print(f"  {target_dir}")
    td = Path(target_dir)
    for label, path in [
        ("docker-compose.yml", td / "docker-compose.yml"),
        ("scripts/", td / "scripts"),
        ("plugins/", td / "plugins"),
    ]:
        if path.exists():
            print(f"  ✓ {label}")
        else:
            (log_error if label == "docker-compose.yml" else log_warn)(f"  ✗ {label} not found")
            if label == "docker-compose.yml":
                issues += 1

    print("\n=== Service Status ===")
    rc = compose_rc(target_dir, "ps", SERVICE_NAME)
    if rc == 0:
        print("✓ Service defined in docker-compose.yml")
    else:
        log_warn("Service not running or not defined")

    print("\n=== Health Check ===")
    print_health(target_dir)

    print("\n=== Summary ===")
    if issues == 0:
        log_info("No issues detected.")
        return 0
    log_error(f"{issues} issue(s) found. Please address them above.")
    return 1


def cmd_update(target_dir: str) -> int:
    """Pull latest changes and conditionally reinstall if relevant files changed."""
    if not shutil.which("git"):
        log_error("git not found. Please install git.")
        return 1

    log_info(f"Repository: {REPO_DIR}")

    if _git_fetch_and_merge() != 0:
        return 1

    log_info("Git pull successful.")

    changed = _get_changed_files()
    relevant = _filter_relevant_files(changed)

    if not relevant:
        log_info("No relevant service files changed — no reinstall needed.")
        return 0

    log_info("Relevant files changed:")
    for f in relevant:
        print(f"  {f}")

    if not _prompt_yes_no("\nReinstall service configuration? [y/N] "):
        log_info("Skipping reinstall.")
        return 0

    if not _rebuild_if_needed(target_dir, relevant):
        return 1

    if not run_install(target_base=target_dir, dry_run=False, no_backup=True):
        log_error("Reinstall failed.")
        return 1

    log_info("Reinstall completed.")
    return _handle_restart_prompt(target_dir)


def cmd_shell(target_dir: str) -> int:
    for shell in ("bash", "sh"):
        rc = compose_rc(target_dir, "exec", SERVICE_NAME, shell, live=True)
        if rc != 126:  # 126 = command not found in container
            return rc
        log_warn(f"{shell} not found in container, trying next...")
    log_error("No usable shell found in container.")
    return 1


def cmd_exec(target_dir: str, exec_cmd: list[str]) -> int:
    if not exec_cmd:
        log_error("Usage: exec <command> [args...]")
        return 1
    return compose_rc(target_dir, "exec", SERVICE_NAME, *exec_cmd, live=True)


def cmd_help() -> int:
    print(f"""
{_c(Colors.GREEN, "cgproxy_ctl.py — Unified lifecycle management for Coingecko Proxy")}

Usage: cgproxy_ctl.py [GLOBAL_OPTS] <command> [CMD_OPTS]

Global Options:
  --target-dir PATH   Target exrproxy-env directory (default: auto-detect)
  -v, --verbose       Enable debug logging
  --dry-run           Preview changes without applying them
  --force             Skip confirmation prompts

Commands:
  install             Install configuration files into target directory
    --no-backup         Skip backup creation
    --deploy            Also start the container after install

  uninstall           Restore from backup, remove files, stop container

  deploy              Start the Docker container
  undeploy            Stop and remove the Docker container
  restart             Restart the service container
  rebuild             Rebuild the Docker image then recreate

  status              Show container status and health check
  logs                Show last 100 log lines
    --follow            Tail logs in real-time

   backup [TAG]        Create a backup (with optional label)
   restore <ID>        Restore from a backup by timestamp or tag
   list-backups        List all available backups

   check               Run full diagnostics
   update              Pull latest git changes; prompt to reinstall if needed
  shell               Open an interactive shell in the container
  exec CMD [ARGS...]  Execute a command in the container

  help                Show this message

Examples:
  cgproxy_ctl.py install --deploy
  cgproxy_ctl.py backup "before-update"
  cgproxy_ctl.py restore "before-update"
  cgproxy_ctl.py logs --follow
  cgproxy_ctl.py exec ls -la
""")
    return 0


# === Helper functions for main() ===


def _resolve_target_dir(target_arg: str | None) -> str | None:
    """Resolve target directory from argument, auto-detection, or None on failure."""
    if target_arg:
        return os.path.abspath(os.path.expanduser(target_arg))
    target_dir = detect_target_dir()
    if target_dir:
        log_info(f"Auto-detected target: {target_dir}")
        return target_dir
    log_error("Cannot auto-detect target directory.")
    log_error("Use --target-dir or set EXRPROXY_ENV.")
    return None


def _validate_target_dir_for_command(command: str, target_dir: str) -> bool:
    """Validate target_dir based on command's requirements."""
    if command in _FULL_COMMANDS:
        return validate_target_dir(target_dir, require_scripts=True)
    if command in _DOCKER_COMMANDS:
        return validate_target_dir(target_dir, require_scripts=False)
    # list-backups and other installer-handled commands: only need dir to exist
    return os.path.isdir(target_dir)


# === Helpers for cmd_update() ===


def _git_fetch_and_merge() -> int:
    """Fetch from origin and fast-forward merge. Returns 0 on success, 1 on error."""
    fetch = subprocess.run(
        ["git", "-C", str(REPO_DIR), "fetch", "origin"],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        log_error(f"git fetch failed: {fetch.stderr.strip()}")
        return 1

    branch_result = subprocess.run(
        ["git", "-C", str(REPO_DIR), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    merge = subprocess.run(
        ["git", "-C", str(REPO_DIR), "merge", "--ff-only", f"origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        log_error("Merge failed — you may have local changes.")
        subprocess.run(["git", "-C", str(REPO_DIR), "status", "--short"])
        return 1
    return 0


def _get_changed_files() -> list[str]:
    """Return list of changed filenames from last git operation (HEAD@{1}..HEAD)."""
    diff = subprocess.run(
        ["git", "-C", str(REPO_DIR), "diff", "--name-only", "HEAD@{1}", "HEAD"],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _filter_relevant_files(changed: list[str]) -> list[str]:
    """Filter files that affect service configuration or code."""
    relevant_prefixes = (
        "install_cg_proxy_xrs.py",
        "cg_proxy_xrs.py",
        "plugins/",
        "Dockerfile",
        "requirements.txt",
    )
    return [f for f in changed if any(f.startswith(p) for p in relevant_prefixes)]


def _needs_rebuild(relevant: list[str]) -> bool:
    """Return True if changes require Docker image rebuild."""
    return any(f.startswith(("cg_proxy_xrs.py", "Dockerfile", "requirements.txt")) for f in relevant)


def _rebuild_if_needed(target_dir: str, relevant: list[str]) -> bool:
    """Rebuild Docker image if needed. Returns True if ok or not needed, False on failure."""
    if not _needs_rebuild(relevant):
        return True
    log_info("Rebuilding Docker image...")
    if not compose(target_dir, "build", SERVICE_NAME):
        log_error("Docker build failed.")
        return False
    log_info("Docker image rebuilt.")
    return True


def _prompt_yes_no(message: str) -> bool:
    """Prompt user for yes/no; return True for yes, False for no (default)."""
    return input(message).strip().lower() in ("y", "yes")


def _handle_restart_prompt(target_dir: str) -> int:
    """Ask user to restart; perform restart if yes. Return service exit code."""
    if _prompt_yes_no("Restart service to apply changes? [y/N] "):
        if compose(target_dir, "up", "-d", SERVICE_NAME):
            log_info("Service restarted.")
            return 0
        else:
            log_error("Failed to restart service.")
            return 1
    return 0


# === Argument Parsing ===


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cgproxy_ctl.py",
        description="Unified lifecycle management for Coingecko Proxy",
        add_help=True,
    )
    parser.add_argument("--target-dir", metavar="PATH", help="Target exrproxy-env directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = False

    # install
    p = sub.add_parser("install", help="Install configuration files")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--deploy", action="store_true", help="Also start container after install")

    # uninstall
    p = sub.add_parser("uninstall", help="Restore from backup and remove plugin files")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")

    # deploy / undeploy / restart / rebuild
    sub.add_parser("deploy", help="Start the Docker container")
    p = sub.add_parser("undeploy", help="Stop and remove the Docker container")
    p.add_argument("--force", action="store_true")
    sub.add_parser("restart", help="Restart the service container")
    sub.add_parser("rebuild", help="Rebuild Docker image and restart")

    # status / logs
    sub.add_parser("status", help="Show container status and health")
    p = sub.add_parser("logs", help="Show container logs")
    p.add_argument("--follow", action="store_true", help="Tail logs in real-time")

    # backup / restore / list-backups
    p = sub.add_parser("backup", help="Create a backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("tag", nargs="?", default=None, metavar="TAG", help="Optional label")

    p = sub.add_parser("restore", help="Restore from a backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("backup_id", metavar="ID", help="Timestamp or tag of the backup")

    sub.add_parser("list-backups", help="List available backups")

    # check / update
    sub.add_parser("check", help="Run diagnostics")
    sub.add_parser("update", help="Pull latest git changes")

    # shell / exec
    sub.add_parser("shell", help="Open interactive shell in the container")
    p = sub.add_parser("exec", help="Execute a command in the container")
    p.add_argument("exec_cmd", nargs=argparse.REMAINDER, metavar="CMD")

    # help
    sub.add_parser("help", help="Show this help message")

    return parser


# === Main ===

# Commands that require docker-compose.yml only
_DOCKER_COMMANDS = {
    "deploy",
    "undeploy",
    "restart",
    "rebuild",
    "status",
    "logs",
    "shell",
    "exec",
    "check",
    "backup",
    "restore",
}
# Commands that also require scripts/start-*.sh
_FULL_COMMANDS = {"install", "uninstall", "update"}


def main() -> int:
    global VERBOSE

    parser = build_parser()
    args = parser.parse_args()

    VERBOSE = args.verbose
    command = args.command or "help"

    if command == "help":
        return cmd_help()

    target_dir = _resolve_target_dir(args.target_dir)
    if target_dir is None:
        return 1

    if not _validate_target_dir_for_command(command, target_dir):
        return 1

    # Build dispatch handlers with closure capturing args and target_dir
    handlers: dict[str, Callable[[], int]] = {
        "install": lambda: cmd_install(target_dir, args.dry_run, args.no_backup, args.deploy),
        "uninstall": lambda: cmd_uninstall(target_dir, args.dry_run, args.force),
        "deploy": lambda: cmd_deploy(target_dir),
        "undeploy": lambda: cmd_undeploy(target_dir, args.force),
        "restart": lambda: cmd_restart(target_dir),
        "rebuild": lambda: cmd_rebuild(target_dir),
        "status": lambda: cmd_status(target_dir),
        "logs": lambda: cmd_logs(target_dir, args.follow),
        "backup": lambda: cmd_backup(target_dir, args.dry_run, args.tag),
        "restore": lambda: cmd_restore(target_dir, args.dry_run, args.force, args.backup_id),
        "list-backups": lambda: cmd_list_backups(target_dir),
        "check": lambda: cmd_check(target_dir),
        "update": lambda: cmd_update(target_dir),
        "shell": lambda: cmd_shell(target_dir),
        "exec": lambda: cmd_exec(target_dir, args.exec_cmd),
    }

    handler = handlers.get(command)
    if handler is None:
        log_error(f"Unknown command: {command}")
        return 1

    return handler()


if __name__ == "__main__":
    sys.exit(main())
