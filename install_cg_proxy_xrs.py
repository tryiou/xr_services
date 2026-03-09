"""
Installer for Coingecko caching proxy XCloud services.

This script deploys cg_coins_list and cg_coins_data plugins to an existing
Blocknet XRouter/XCloud node installation. It modifies configuration files
in a safe, idempotent manner.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TypedDict

import yaml

# === Constants ===
PLUGINS = ["cg_coins_list", "cg_coins_data"]

# RPC configuration for each plugin
RPC_CONFIGS = {
    "cg_coins_data": {
        "host": "xr_service_cg_proxy",
        "port": "8080",
        "user": "A",
        "pass": "B",
        "method": "cg_coins_data",
    },
    "cg_coins_list": {
        "host": "xr_service_cg_proxy",
        "port": "8080",
        "user": "A",
        "pass": "B",
        "method": "cg_coins_list",
    },
}

# Docker compose service definition
# Use absolute path to this repository's directory as build context
_xr_services_dir = os.path.dirname(os.path.abspath(__file__))

DOCKER_SERVICE = {
    "xr_service_cg_proxy": {
        "build": {"context": _xr_services_dir, "dockerfile": "Dockerfile"},
        "restart": "no",
        "stop_signal": "SIGINT",
        "stop_grace_period": "5m",
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "2m", "max-file": "10"},
        },
        "networks": {"backend": {"ipv4_address": "172.31.11.3"}},
    }
}

DOCKER_MARKER = "#### END UTXO STACK ####"

# Plugin configuration files
PLUGIN_FILES = ["cg_coins_list.conf", "cg_coins_data.conf"]


def setup_logging() -> None:
    """Configure logging for standalone script execution."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# === Type Definitions ===
class BackupFileInfo(TypedDict):
    """Information about a single file in a backup manifest."""

    path: str
    existed: bool
    action: str  # "modified" or "created"


class BackupManifest(TypedDict):
    """Complete backup manifest structure."""

    timestamp: str
    backup_dir: str
    target_dir: str
    created_at: str
    files: list[BackupFileInfo]


class InstallerError(Exception):
    """Base exception for installer failures."""

    pass


def validate_target_base(target_base: str) -> None:
    """
    Validate that target_base is a valid exrproxy-env directory.
    Raises InstallerError if validation fails.
    """
    if not os.path.isdir(target_base):
        raise InstallerError(f"Target directory does not exist: {target_base}")

    docker_compose = os.path.join(target_base, "docker-compose.yml")
    if not os.path.isfile(docker_compose):
        raise InstallerError(f"docker-compose.yml not found in {target_base} (not a valid exrproxy-env)")


# === Global Path Variables (set by initialize_paths) ===
PLUGINS_SRC_DIR: str = ""
PLUGINS_DST_DIR: str = ""
SCRIPTS_DIR: str = ""
START_XRPROXY: str = ""
START_SNODE: str = ""
DOCKER_COMPOSE: str = ""
BACKUP_DIR: str = ""  # Set during install if backup enabled

# Files that will be modified/created by installer (relative to target_base)
FILES_TO_MODIFY = [
    "plugins/cg_coins_list.conf",
    "plugins/cg_coins_data.conf",
    "scripts/start-xrproxy.sh",
    "scripts/start-snode.sh",
    "docker-compose.yml",
]


def initialize_paths(target_base: str) -> None:
    """
    Initialize global path variables based on target base directory.

    Args:
        target_base: Path to exrproxy-env root (e.g., ~/exrproxy-env)
    """
    global PLUGINS_SRC_DIR, PLUGINS_DST_DIR, SCRIPTS_DIR, START_XRPROXY, START_SNODE, DOCKER_COMPOSE

    # Source plugin files are in our repository's plugins/ directory
    PLUGINS_SRC_DIR = os.path.join(os.path.dirname(__file__), "plugins")

    # Destination paths are relative to target_base
    PLUGINS_DST_DIR = os.path.join(target_base, "plugins")
    SCRIPTS_DIR = os.path.join(target_base, "scripts")
    START_XRPROXY = os.path.join(SCRIPTS_DIR, "start-xrproxy.sh")
    START_SNODE = os.path.join(SCRIPTS_DIR, "start-snode.sh")
    DOCKER_COMPOSE = os.path.join(target_base, "docker-compose.yml")


# === Shared Utilities ===


def ensure_dir(path: str) -> None:
    """Create directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)
    logging.debug(f"Ensured directory exists: {path}")


def create_backup(target_base: str, files: list[str]) -> str:
    """
    Create a timestamped backup of files that will be modified.
    Returns the backup directory path.
    """
    backup_root = os.path.join(target_base, ".backups")
    ensure_dir(backup_root)

    # Use milliseconds for uniqueness
    timestamp = str(int(time.time() * 1000))
    backup_dir = os.path.join(backup_root, timestamp)
    ensure_dir(backup_dir)

    manifest = {
        "timestamp": timestamp,
        "backup_dir": backup_dir,
        "target_dir": target_base,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": [],
    }

    logging.info(f"Creating backup in {backup_dir}")

    for rel_path in files:
        src_path = os.path.join(target_base, rel_path)
        dst_path = os.path.join(backup_dir, rel_path)

        file_info = {"path": rel_path, "existed": os.path.exists(src_path)}

        if os.path.exists(src_path):
            # Create parent directory in backup
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            # Copy file with metadata
            shutil.copy2(src_path, dst_path)
            logging.debug(f"Backed up {rel_path}")
            file_info["action"] = "modified"
        else:
            # Track that this file will be created
            file_info["action"] = "created"
            # Create a marker for non-existing files
            marker_path = os.path.join(backup_dir, f".created_{os.path.basename(rel_path)}")
            open(marker_path, "w").close()

        manifest["files"].append(file_info)

    # Write manifest
    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Update 'latest' symlink
    latest_link = os.path.join(backup_root, "latest")
    if os.path.exists(latest_link) or os.path.islink(latest_link):
        os.unlink(latest_link)
    os.symlink(timestamp, latest_link)

    logging.info(f"Backup created successfully ({len(files)} files tracked)")
    return backup_dir


def restore_backup(backup_dir: str) -> bool:
    """
    Restore target directory from backup.
    Returns True if successful, False otherwise.
    """
    if not os.path.isdir(backup_dir):
        logging.error(f"Backup directory not found: {backup_dir}")
        return False

    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        logging.error(f"Manifest not found in backup: {backup_dir}")
        return False

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as e:
        logging.error(f"Failed to read manifest: {e}")
        return False

    target_base = manifest.get("target_dir")
    if not target_base or not os.path.isdir(target_base):
        logging.error(f"Invalid target directory in manifest: {target_base}")
        return False

    logging.info(f"Restoring from backup {backup_dir}")

    for file_info in manifest.get("files", []):
        rel_path = file_info["path"]
        src_path = os.path.join(backup_dir, rel_path)
        dst_path = os.path.join(target_base, rel_path)

        try:
            if file_info.get("existed", True):
                # File existed before backup, restore it
                if os.path.exists(src_path):
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)
                    logging.info(f"Restored {rel_path}")
                else:
                    logging.warning(f"Backup file missing: {src_path}")
            else:
                # File was created during install, delete it if it exists
                if os.path.exists(dst_path):
                    os.remove(dst_path)
                    logging.info(f"Removed created file {rel_path}")
        except Exception as e:
            logging.error(f"Failed to restore {rel_path}: {e}")
            return False

    logging.info("Restore completed successfully")
    return True


def find_latest_backup(target_base: str) -> str | None:
    """
    Find the most recent backup directory in target_base/.backups/.
    Returns absolute path or None if no backups exist.
    """
    backup_root = os.path.join(target_base, ".backups")
    if not os.path.isdir(backup_root):
        return None

    # Check for 'latest' symlink first
    latest_link = os.path.join(backup_root, "latest")
    if os.path.islink(latest_link):
        resolved = os.path.realpath(latest_link)
        if os.path.isdir(resolved):
            return resolved

    # Fallback: scan for timestamp directories, return newest
    backups = []
    for entry in os.listdir(backup_root):
        entry_path = os.path.join(backup_root, entry)
        if os.path.isdir(entry_path) and entry.isdigit():
            try:
                timestamp = int(entry)
                backups.append((timestamp, entry_path))
            except ValueError:
                continue

    if not backups:
        return None

    # Return most recent
    backups.sort(reverse=True)
    return backups[0][1]


def find_oldest_backup(target_base: str) -> str | None:
    """
    Find the oldest (first) backup directory in target_base/.backups/.
    Used for uninstall to restore to the pre-first-install state.
    Returns absolute path or None if no backups exist.
    """
    backup_root = os.path.join(target_base, ".backups")
    if not os.path.isdir(backup_root):
        return None

    # Scan for timestamp directories, return oldest
    backups = []
    for entry in os.listdir(backup_root):
        entry_path = os.path.join(backup_root, entry)
        if os.path.isdir(entry_path) and entry.isdigit():
            try:
                timestamp = int(entry)
                backups.append((timestamp, entry_path))
            except ValueError:
                continue

    if not backups:
        return None

    # Return oldest (smallest timestamp)
    backups.sort()
    return backups[0][1]


def list_backups(target_base: str) -> None:
    """
    List all available backups in target_base/.backups/.
    """
    backup_root = os.path.join(target_base, ".backups")
    if not os.path.isdir(backup_root):
        logging.info("No backups directory found")
        return

    # Load tags if available
    tags_file = os.path.join(backup_root, "tags")
    tags = {}
    if os.path.exists(tags_file):
        try:
            with open(tags_file) as f:
                tags = json.load(f)
        except Exception:
            pass

    backups = []
    for entry in os.listdir(backup_root):
        entry_path = os.path.join(backup_root, entry)
        if os.path.isdir(entry_path) and entry.isdigit():
            manifest_path = os.path.join(entry_path, "manifest.json")
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path) as f:
                        manifest = json.load(f)
                    timestamp = manifest.get("timestamp", entry)
                    created = manifest.get("created_at", "unknown")
                    files = manifest.get("files", [])
                    modified = sum(1 for f in files if f.get("existed", True))
                    created_count = sum(1 for f in files if not f.get("existed", True))
                    is_latest = (
                        os.path.islink(os.path.join(backup_root, "latest")) and os.path.realpath(os.path.join(backup_root, "latest")) == entry_path
                    )
                    tag = tags.get(entry, "")
                    backups.append(
                        {
                            "timestamp": timestamp,
                            "created": created,
                            "total": len(files),
                            "modified": modified,
                            "created_count": created_count,
                            "is_latest": is_latest,
                            "tag": tag,
                            "path": entry_path,
                        }
                    )
                except Exception as e:
                    logging.warning(f"Failed to read manifest for {entry}: {e}")

    if not backups:
        logging.info("No backups found")
        return

    # Sort by timestamp descending
    backups.sort(key=lambda x: x["timestamp"], reverse=True)

    # Print table
    logging.info("Available backups:")
    logging.info("-" * 80)
    for b in backups:
        latest_mark = " (LATEST)" if b["is_latest"] else ""
        tag_mark = f" [{b['tag']}]" if b["tag"] else ""
        date_str = b["created"].split("T")[0] if "T" in b["created"] else b["created"]
        logging.info(
            f"Timestamp: {b['timestamp']} | Date: {date_str} | "
            f"Files: {b['total']} (mod:{b['modified']} new:{b['created_count']}){latest_mark}{tag_mark}"
        )
    logging.info("-" * 80)


def _remove_plugin_files(target_base: str) -> None:
    """Remove plugin configuration files if they exist."""
    plugins_dir = os.path.join(target_base, "plugins")
    for filename in ["cg_coins_list.conf", "cg_coins_data.conf"]:
        filepath = os.path.join(plugins_dir, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                logging.info(f"Removed plugin file: {filename}")
            except Exception as e:
                logging.warning(f"Failed to remove {filepath}: {e}")


def _stop_docker_service_compose(compose_file: str) -> None:
    """Stop and remove the xr_service_cg_proxy service via docker-compose."""
    if not os.path.exists(compose_file):
        return
    try:
        result = subprocess.run(
            ["docker-compose", "-f", compose_file, "down", "xr_service_cg_proxy"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.info("Stopped and removed Docker container")
        else:
            stderr_lower = result.stderr.lower()
            if any(x in stderr_lower for x in ("no such", "not defined", "not found")):
                logging.info("Service not defined in docker-compose.yml")
            else:
                logging.warning(f"docker-compose down failed: {result.stderr.strip()}")
    except FileNotFoundError:
        logging.warning("docker-compose not found - cannot stop container via compose")
    except Exception as e:
        logging.warning(f"docker-compose down error: {e}")


def _force_remove_docker_container() -> None:
    """Force remove any Docker container matching the service name."""
    try:
        result = subprocess.run(["docker", "ps", "-aq", "--filter", "name=xr_service_cg_proxy"], capture_output=True, text=True)
        ids = result.stdout.strip().split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
            logging.info(f"Force-removed {len(ids)} Docker container(s)")
    except FileNotFoundError:
        pass  # docker not available
    except Exception as e:
        logging.debug(f"Fallback container removal error: {e}")


def uninstall(target_base: str) -> int:
    """
    Uninstall the Coingecko proxy service.
    Restores from the oldest backup (pre-first-install state) and removes plugin configs.
    Returns exit code (0=success, 1=error).
    """
    try:
        backup_dir = find_oldest_backup(target_base)
        if not backup_dir:
            logging.error("No backup found - cannot uninstall")
            logging.error("Run a normal install first, or manually remove files")
            return 1

        logging.info(f"Restoring from backup: {backup_dir}")

        if not restore_backup(backup_dir):
            logging.error("Restore failed")
            return 1

        _remove_plugin_files(target_base)

        compose_file = os.path.join(target_base, "docker-compose.yml")
        _stop_docker_service_compose(compose_file)
        _force_remove_docker_container()

        logging.info("Uninstall completed successfully")
        return 0

    except Exception as e:
        logging.error(f"Uninstall failed: {e}", exc_info=True)
        return 1


def atomic_write(path: str, lines: list[str]) -> None:
    """Write to file atomically using temp file then rename."""
    dir_name = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dir_name, delete=False) as tmp:
        tmp.writelines(lines)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_path = tmp.name
    os.replace(temp_path, path)
    logging.debug(f"Atomic write completed: {path}")


def parse_plugins_line(line: str) -> list[str]:
    """
    Parse a plugins line to extract plugin names.
    Handles both formats:
    - 'set-ph = PLUGINS=plugin1,plugin2' (from start-xrproxy.sh)
    - 'plugins=plugin1,plugin2' (from start-snode.sh)
    """
    # Find last '=' to get value part
    idx = line.rfind("=")
    if idx == -1:
        return []
    value = line[idx + 1 :].strip()
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def merge_plugins(current: list[str], required: list[str]) -> tuple[list[str], bool]:
    """
    Merge required plugins into current list.
    Returns (new_list, changed).
    """
    new_list = current.copy()
    changed = False
    for plugin in required:
        if plugin not in new_list:
            new_list.append(plugin)
            changed = True
    return new_list, changed


# === File Modification Functions ===


def copy_plugin_files(dry_run: bool = False) -> None:
    """Copy plugin .conf files from repository to user's plugins directory."""
    ensure_dir(PLUGINS_DST_DIR)

    missing = []
    for plugin_file in PLUGIN_FILES:
        src = os.path.join(PLUGINS_SRC_DIR, plugin_file)
        dst = os.path.join(PLUGINS_DST_DIR, plugin_file)
        if os.path.exists(src):
            if dry_run:
                logging.info(f"[DRY RUN] Would copy {src} -> {dst}")
            else:
                shutil.copy2(src, dst)
                logging.info(f"Copied {plugin_file} to {dst}")
        else:
            missing.append(src)

    if missing:
        raise FileNotFoundError(f"Missing required plugin source files: {', '.join(missing)}")


# === Helpers for update_start_xrproxy_rpc_config ===


def _find_eol_markers(content: str) -> tuple[int, int]:
    """Find the first and second 'EOL' markers. Returns (first, second) indices."""
    first_eol = content.find("EOL")
    if first_eol == -1:
        raise InstallerError("No 'EOL' markers found in start-xrproxy.sh")
    second_eol = content.find("EOL", first_eol + 1)
    if second_eol == -1:
        raise InstallerError("Second 'EOL' marker not found in start-xrproxy.sh")
    return first_eol, second_eol


def _parse_existing_rpc_settings(content: str) -> dict[tuple[str, str], str]:
    """Parse existing RPC_* settings from file content into {(plugin, type): value}."""
    existing_settings: dict[tuple[str, str], str] = {}
    rpc_pattern = re.compile(r"^set-ph\s*=\s*RPC_(\w+)_(HOSTIP|PORT|USER|PASS|METHOD)=(.+)$")
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = rpc_pattern.match(stripped)
        if match:
            plugin, setting_type, value = match.groups()
            existing_settings[(plugin, setting_type)] = value.strip()
    return existing_settings


def _determine_plugins_to_update(existing_settings: dict[tuple[str, str], str]) -> set[str]:
    """Determine which plugins' RPC settings differ from expected config."""
    SETTING_TYPES = ["HOSTIP", "PORT", "USER", "PASS", "METHOD"]
    CONFIG_KEYS = {"HOSTIP": "host", "PORT": "port", "USER": "user", "PASS": "pass", "METHOD": "method"}
    plugins_to_update: set[str] = set()
    for plugin in PLUGINS:
        config = RPC_CONFIGS[plugin]
        for st in SETTING_TYPES:
            expected = config[CONFIG_KEYS[st]]
            actual = existing_settings.get((plugin, st))
            if actual != expected:
                plugins_to_update.add(plugin)
                break
    return plugins_to_update


def _build_rpc_block(plugins: set[str]) -> str:
    """Build the RPC configuration block string for the given plugins."""
    rpc_lines = []
    for plugin in sorted(plugins):
        cfg = RPC_CONFIGS[plugin]
        rpc_lines.extend(
            [
                f"set-ph = RPC_{plugin}_HOSTIP={cfg['host']}\n",
                f"set-ph = RPC_{plugin}_PORT={cfg['port']}\n",
                f"set-ph = RPC_{plugin}_USER={cfg['user']}\n",
                f"set-ph = RPC_{plugin}_PASS={cfg['pass']}\n",
                f"set-ph = RPC_{plugin}_METHOD={cfg['method']}\n",
                "\n",
            ]
        )
    return "".join(rpc_lines).strip()


def update_start_xrproxy_rpc_config(dry_run: bool = False) -> bool:
    """
    Add RPC configuration lines for our services to start-xrproxy.sh.
    Returns True if file would be modified or was modified.
    Raises InstallerError if the file is missing or malformed.
    """
    if not os.path.exists(START_XRPROXY):
        raise InstallerError(f"Required file not found: {START_XRPROXY}")

    with open(START_XRPROXY) as f:
        content = f.read()

    _, second_eol = _find_eol_markers(content)
    existing_settings = _parse_existing_rpc_settings(content)
    plugins = _determine_plugins_to_update(existing_settings)

    if not plugins:
        logging.info("All RPC settings already present and correct")
        return False

    rpc_block = _build_rpc_block(plugins)

    if dry_run:
        plugins_str = ", ".join(sorted(plugins))
        logging.info(f"[DRY RUN] Would insert/update RPC config for: {plugins_str}")
        return True

    updated = content[:second_eol] + "\n\n" + rpc_block + "\n" + content[second_eol:]
    if not updated.endswith("\n"):
        updated += "\n"

    atomic_write(START_XRPROXY, updated.splitlines(keepends=True))
    logging.info(f"Updated RPC config for {', '.join(sorted(plugins))} in {START_XRPROXY}")
    return True


def update_start_xrproxy_plugins(dry_run: bool = False) -> bool:
    """
    Ensure PLUGINS variable in start-xrproxy.sh includes our plugins.
    Returns True if file was modified.
    Raises InstallerError if the file is missing or PLUGINS line not found.
    """
    if not os.path.exists(START_XRPROXY):
        raise InstallerError(f"Required file not found: {START_XRPROXY}")

    with open(START_XRPROXY) as f:
        lines = f.readlines()

    modified = False
    for i, line in enumerate(lines):
        if line.strip().startswith("set-ph = PLUGINS="):
            current_plugins = parse_plugins_line(line)
            new_plugins, _ = merge_plugins(current_plugins, PLUGINS)
            # Build canonical line
            new_line = f"set-ph = PLUGINS={','.join(new_plugins)}\n"
            # Check if line needs update (format or content)
            if line != new_line:
                lines[i] = new_line
                modified = True
                logging.info(f"Updated PLUGINS line to include {', '.join(PLUGINS)}")
            else:
                logging.info("PLUGINS already contains all required plugins and correct format")
            break
    else:
        raise InstallerError("PLUGINS line not found in start-xrproxy.sh")

    if modified:
        if dry_run:
            logging.info(f"[DRY RUN] Would write updated {START_XRPROXY}")
            return True
        atomic_write(START_XRPROXY, lines)
        logging.info(f"Updated {START_XRPROXY}")

    return modified


def _find_xrouter_section(lines: list[str]) -> tuple[int, int]:
    """Find the start and end indices of the xrouter.conf heredoc section."""
    section_start = -1
    section_end = -1
    for i, line in enumerate(lines):
        if "cat > /opt/blockchain/data/xrouter.conf << EOL" in line:
            section_start = i
        if section_start != -1 and section_end == -1 and line.strip() == "EOL":
            section_end = i
            break
    if section_start == -1 or section_end == -1:
        raise InstallerError("xrouter.conf section not found in start-snode.sh")
    return section_start, section_end


def _find_plugins_line(lines: list[str], section_start: int, section_end: int) -> int:
    """Find the index of a plugins= line within the section. Returns -1 if not found."""
    for i in range(section_start, section_end):
        if lines[i].strip().startswith("plugins="):
            return i
    return -1


def _determine_insert_position(lines: list[str], section_start: int, section_end: int) -> int:
    """Determine where to insert a new plugins= line (after wallets= or host=, or after section start)."""
    # Prefer after wallets=
    for i in range(section_start, section_end):
        stripped = lines[i].strip()
        if stripped.startswith("wallets="):
            return i + 1
    # Then after host=
    for i in range(section_start, section_end):
        if lines[i].strip().startswith("host="):
            return i + 1
    # Default: after section start line
    return section_start + 1


def modify_start_snode_plugins(dry_run: bool = False) -> bool:
    """
    Inject plugin names into xrouter.conf section in start-snode.sh.
    Returns True if file was modified.
    Raises InstallerError if the file is missing or xrouter.conf section not found.
    """
    if not os.path.exists(START_SNODE):
        raise InstallerError(f"Required file not found: {START_SNODE}")

    with open(START_SNODE) as f:
        lines = f.readlines()

    section_start, section_end = _find_xrouter_section(lines)
    plugins_idx = _find_plugins_line(lines, section_start, section_end)
    modified = False

    if plugins_idx != -1:
        # Update existing plugins line
        current_plugins = parse_plugins_line(lines[plugins_idx])
        new_plugins, changed = merge_plugins(current_plugins, PLUGINS)
        if not changed:
            logging.info("Plugins already includes all required plugins")
            return False
        lines[plugins_idx] = f"plugins={','.join(new_plugins)}\n"
        modified = True
        logging.info(f"Updated plugins line in xrouter.conf to include {', '.join(PLUGINS)}")
    else:
        # Insert new plugins line
        insert_idx = _determine_insert_position(lines, section_start, section_end)
        new_line = f"plugins={','.join(PLUGINS)}\n"
        if dry_run:
            logging.info(f"[DRY RUN] Would insert '{new_line.strip()}' at line {insert_idx} in {START_SNODE}")
            return True
        lines.insert(insert_idx, new_line)
        logging.info("Added plugins line to xrouter.conf section in start-snode.sh")
        modified = True

    if modified and not dry_run:
        atomic_write(START_SNODE, lines)
        logging.info(f"Updated {START_SNODE}")

    return modified


def _load_and_validate_docker_yaml(docker_compose: str) -> dict:
    """Load and validate the docker-compose YAML file."""
    with open(docker_compose) as f:
        docker_data = yaml.safe_load(f)
    if not isinstance(docker_data, dict):
        raise InstallerError(f"{docker_compose}: invalid YAML (expected mapping)")
    return docker_data


def _find_service_marker(lines: list[str], marker: str) -> int:
    """Find the index of the marker line in the file."""
    for i, line in enumerate(lines):
        if marker in line:
            return i
    return -1


def _generate_indented_service_yaml() -> list[str]:
    """Generate the indented YAML lines for the xr_service_cg_proxy service."""
    service_yaml = yaml.dump(DOCKER_SERVICE, default_flow_style=False, sort_keys=False).strip()
    # yaml.dump gives 2-space indent for nested dicts; need to add 2 more for top-level
    return ["  " + line if line.strip() else line for line in service_yaml.splitlines()]


def modify_docker_compose(dry_run: bool = False) -> bool:
    """
    Add the xr_service_cg_proxy service to docker-compose.yml after marker.
    Returns True if file was or would be modified.
    Raises InstallerError if the file is missing or invalid YAML.
    """
    if not os.path.exists(DOCKER_COMPOSE):
        raise InstallerError(f"Required file not found: {DOCKER_COMPOSE}")

    docker_data = _load_and_validate_docker_yaml(DOCKER_COMPOSE)

    # Check if service already exists with exact config
    existing = docker_data.get("services", {}).get("xr_service_cg_proxy")
    if existing == DOCKER_SERVICE["xr_service_cg_proxy"]:
        logging.info("xr_service_cg_proxy service already configured correctly")
        return False

    if existing:
        logging.warning("xr_service_cg_proxy exists but with different config - not overwriting")
        return False

    # Add service
    if "services" not in docker_data:
        docker_data["services"] = {}
    docker_data["services"].update(DOCKER_SERVICE)

    if dry_run:
        logging.info(f"[DRY RUN] Would update {DOCKER_COMPOSE} with xr_service_cg_proxy service")
        return True

    # Read original file to preserve formatting and insert after marker
    with open(DOCKER_COMPOSE) as f:
        lines = f.readlines()

    marker_idx = _find_service_marker(lines, DOCKER_MARKER)
    if marker_idx == -1:
        logging.warning(f"Marker '{DOCKER_MARKER}' not found; appending at end")
        with open(DOCKER_COMPOSE, "w") as f:
            yaml.dump(docker_data, f, default_flow_style=False, sort_keys=False)
        logging.info(f"Updated {DOCKER_COMPOSE}")
        return True

    indented_lines = _generate_indented_service_yaml()
    if dry_run:
        logging.info(f"[DRY RUN] Would insert service after line {marker_idx} in {DOCKER_COMPOSE}")
        return True

    lines.insert(marker_idx + 1, "\n" + "\n".join(indented_lines) + "\n")
    atomic_write(DOCKER_COMPOSE, lines)
    logging.info(f"Updated {DOCKER_COMPOSE}")
    return True


# === Main Entry Point ===


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Install Coingecko proxy services into an exrproxy-env directory")
    parser.add_argument(
        "--target-dir",
        help="Path to exrproxy-env root (default: parent directory of this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what changes would be made without modifying any files",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup creation (use with caution - no automatic restore on failure)",
    )
    parser.add_argument("--list-backups", action="store_true", help="List available backups and exit")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall by restoring from backup, removing plugin files, and stopping container",
    )
    parser.add_argument(
        "--create-backup",
        action="store_true",
        help="Create backup of tracked files and exit",
    )
    parser.add_argument(
        "--restore",
        metavar="BACKUP_DIR",
        help="Restore files from specified backup directory (timestamp or path) and exit",
    )
    parser.add_argument(
        "--tag",
        metavar="TAG",
        help="Optional tag for backup (used with --create-backup)",
    )
    return parser.parse_args()


# === Public API (for direct import by cgproxy_ctl.py) ===


def _prepare_backup(target_base: str, dry_run: bool, no_backup: bool) -> str | None:
    """Create backup if needed. Returns backup_dir or None if skipped/failed."""
    if dry_run or no_backup:
        return None
    try:
        return create_backup(target_base, FILES_TO_MODIFY)
    except Exception as e:
        logging.error(f"Failed to create backup: {e}")
        return None


def run_install(target_base: str, dry_run: bool, no_backup: bool) -> int:
    """
    Install the Coingecko proxy service.

    Args:
        target_base: Path to exrproxy-env root directory
        dry_run: If True, show changes without applying
        no_backup: If True, skip backup creation

    Returns:
        0 on success, 1 on failure
    """
    try:
        validate_target_base(target_base)
    except InstallerError as e:
        logging.error(f"Invalid target directory: {e}")
        return 1

    initialize_paths(target_base)

    logging.info("=== Coingecko Proxy Installer ===")
    logging.info(f"Target directory: {target_base}")
    if dry_run:
        logging.info("=== DRY RUN MODE - no changes will be made ===")

    backup_dir = _prepare_backup(target_base, dry_run, no_backup)
    if backup_dir is None and not (dry_run or no_backup):
        # _prepare_backup already logged the error
        return 1

    try:
        logging.info("\n--- Stage 1: Copying plugin configuration files ---")
        copy_plugin_files(dry_run)

        logging.info("\n--- Stage 2: Adding RPC configuration to start-xrproxy.sh ---")
        update_start_xrproxy_rpc_config(dry_run)

        logging.info("\n--- Stage 3: Updating PLUGINS in start-xrproxy.sh ---")
        update_start_xrproxy_plugins(dry_run)

        logging.info("\n--- Stage 4: Injecting plugins into start-snode.sh ---")
        modify_start_snode_plugins(dry_run)

        logging.info("\n--- Stage 5: Updating docker-compose.yml ---")
        modify_docker_compose(dry_run)

        logging.info("\n=== Installation completed successfully ===")
        if backup_dir:
            logging.info(f"Backup preserved at {backup_dir}")
        return 0

    except Exception as e:
        logging.error(f"Installation failed: {e}", exc_info=True)
        if backup_dir:
            logging.info("Attempting to restore from backup...")
            if restore_backup(backup_dir):
                logging.info("System restored to pre-install state.")
                logging.info(f"Backup kept at {backup_dir} for inspection")
            else:
                logging.error("Restore failed! Manual intervention required.")
        return 1


def run_uninstall(target_base: str, dry_run: bool, _force: bool) -> int:
    """Uninstall the service by restoring from backup and removing files."""
    # Validate target directory
    try:
        validate_target_base(target_base)
    except InstallerError as e:
        logging.error(f"Invalid target directory: {e}")
        return 1

    initialize_paths(target_base)
    if dry_run:
        logging.warning("Dry-run not implemented for uninstall; proceeding with actual uninstall")
    return uninstall(target_base)


def run_backup(target_base: str, tag: str | None = None, dry_run: bool = False) -> int:
    """Create a backup of tracked files."""
    # Validate target directory
    try:
        validate_target_base(target_base)
    except InstallerError as e:
        logging.error(f"Invalid target directory: {e}")
        return 1

    initialize_paths(target_base)

    # In dry-run mode, just show what would be backed up
    if dry_run:
        logging.info("=== DRY RUN: Backup ===")
        logging.info(f"Target directory: {target_base}")
        logging.info(f"Files that would be backed up ({len(FILES_TO_MODIFY)}):")
        for rel_path in FILES_TO_MODIFY:
            full_path = os.path.join(target_base, rel_path)
            status = "✓ exists" if os.path.exists(full_path) else "✗ missing"
            logging.info(f"  {rel_path} [{status}]")
        backup_dir = os.path.join(target_base, ".backups", "DRY_RUN_TIMESTAMP")
        logging.info(f"Backup would be created at: {backup_dir}")
        if tag:
            logging.info(f"Backup would be tagged: {tag}")
        return 0

    try:
        backup_dir = create_backup(target_base, FILES_TO_MODIFY)
        if tag:
            tags_file = os.path.join(target_base, ".backups", "tags")
            try:
                tags = {}
                if os.path.exists(tags_file):
                    with open(tags_file) as f:
                        tags = json.load(f)
                timestamp = os.path.basename(backup_dir)
                tags[timestamp] = tag
                with open(tags_file, "w") as f:
                    json.dump(tags, f, indent=2)
                logging.info(f"Backup tagged: {tag}")
            except Exception as e:
                logging.warning(f"Failed to save tag: {e}")
        logging.info(f"Backup created: {backup_dir}")
        return 0
    except Exception as e:
        logging.error(f"Backup failed: {e}", exc_info=True)
        return 1


def _resolve_backup_path(target_base: str, backup_id: str) -> str | None:
    """
    Resolve backup_id (timestamp, tag, or path) to an absolute backup directory path.
    Returns None if not found.
    """
    # If absolute path and exists, use it directly
    if os.path.isabs(backup_id) and os.path.isdir(backup_id):
        return backup_id

    # If backup_id itself is a timestamp directory (all digits)
    if backup_id.isdigit():
        path = os.path.join(target_base, ".backups", backup_id)
        if os.path.isdir(path):
            return path
        else:
            logging.error(f"Backup directory not found: {path}")
            return None

    # Treat as tag: look up in tags file
    tags_file = os.path.join(target_base, ".backups", "tags")
    if os.path.exists(tags_file):
        try:
            with open(tags_file) as f:
                tags = json.load(f)
            for ts, tag in tags.items():
                if tag == backup_id:
                    path = os.path.join(target_base, ".backups", ts)
                    if os.path.isdir(path):
                        return path
                    else:
                        logging.error(f"Backup directory missing for tag: {path}")
                        return None
            logging.error(f"Tag not found: {backup_id}")
            return None
        except Exception as e:
            logging.error(f"Failed to read tags file: {e}")
            return None
    else:
        logging.error("No tags file found - cannot resolve tag")
        return None


def run_restore(target_base: str, backup_id: str, dry_run: bool = False) -> int:
    """Restore from a backup by timestamp or tag."""
    try:
        validate_target_base(target_base)
    except InstallerError as e:
        logging.error(f"Invalid target directory: {e}")
        return 1

    initialize_paths(target_base)

    backup_path = _resolve_backup_path(target_base, backup_id)
    if not backup_path:
        return 1

    if dry_run:
        logging.info(f"[DRY RUN] Would restore from {backup_path}")
        return 0

    if restore_backup(backup_path):
        logging.info("Restore completed successfully")
        return 0
    else:
        logging.error("Restore failed")
        return 1


def run_list_backups(target_base: str) -> int:
    """List all available backups."""
    try:
        validate_target_base(target_base)
    except InstallerError as e:
        logging.error(f"Invalid target directory: {e}")
        return 1

    initialize_paths(target_base)
    list_backups(target_base)
    return 0


def main() -> int:
    """
    Run installer. Returns exit code (0=success, 1=error).
    """
    setup_logging()
    args = parse_args()

    # Determine target base directory
    if args.target_dir:
        target_base = os.path.abspath(os.path.expanduser(args.target_dir))
    else:
        # Default: parent directory of the repository (i.e., script is in xr_services/, target is parent)
        target_base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # Dispatch to appropriate function based on arguments
    if args.create_backup:
        return run_backup(target_base, args.tag, args.dry_run)
    elif args.restore:
        # run_restore also handles dry-run
        return run_restore(target_base, args.restore, args.dry_run)
    elif args.list_backups:
        return run_list_backups(target_base)
    elif args.uninstall:
        return run_uninstall(target_base, args.dry_run, args.force)
    else:
        # Default: install
        return run_install(target_base, args.dry_run, args.no_backup)


if __name__ == "__main__":
    sys.exit(main())
