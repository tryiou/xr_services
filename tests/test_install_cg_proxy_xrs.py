"""

Tests for install_cg_proxy_xrs.py - Installer and backup/restore operations.

Focused on:
- Backup/restore correctness (critical safety feature)
- File modification logic (string manipulation, YAML handling)
- Validation and error handling
- Idempotency (can run multiple times safely)
"""

import json
import logging
import os

import pytest
import yaml

from install_cg_proxy_xrs import (
    InstallerError,
    _determine_insert_position,
    _find_plugins_line,
    _find_xrouter_section,
    _remove_plugin_files,
    create_backup,
    find_latest_backup,
    find_oldest_backup,
    initialize_paths,
    list_backups,
    merge_plugins,
    modify_docker_compose,
    modify_start_snode_plugins,
    parse_plugins_line,
    restore_backup,
    update_start_xrproxy_rpc_config,
    validate_target_base,
)

# === Utility Function Tests ===


def test_parse_plugins_line_set_ph_format():
    """Test parsing 'set-ph = PLUGINS=plugin1,plugin2' format."""
    line = "set-ph = PLUGINS=cg_coins_list,cg_coins_data"
    result = parse_plugins_line(line)
    assert result == ["cg_coins_list", "cg_coins_data"]


def test_parse_plugins_line_plugins_equals_format():
    """Test parsing 'plugins=plugin1,plugin2' format."""
    line = "plugins=walletnotifier,someplugin"
    result = parse_plugins_line(line)
    assert result == ["walletnotifier", "someplugin"]


def test_parse_plugins_line_empty():
    """Test parsing empty value returns empty list."""
    assert parse_plugins_line("set-ph = PLUGINS=") == []
    assert parse_plugins_line("plugins=") == []


def test_parse_plugins_line_with_spaces():
    """Test parsing handles spaces around plugin names."""
    line = "plugins=plugin1 , plugin2 ,  plugin3"
    result = parse_plugins_line(line)
    assert result == ["plugin1", "plugin2", "plugin3"]


def test_merge_plugins_adds_new():
    """Test merge_plugins adds plugins not already present."""
    current = ["a", "b"]
    new, changed = merge_plugins(current, ["b", "c"])
    assert new == ["a", "b", "c"]
    assert changed is True


def test_merge_plugins_no_change():
    """Test merge_plugins returns False when nothing changes."""
    current = ["a", "b", "c"]
    new, changed = merge_plugins(current, ["b", "a"])
    assert new == current
    assert changed is False


def test_merge_plugins_preserves_order():
    """Test merge_plugins preserves existing order, appends new."""
    current = ["x", "y", "z"]
    new, _ = merge_plugins(current, ["a", "b"])
    assert new == ["x", "y", "z", "a", "b"]


# === Backup and Restore Tests ===


def test_create_backup_copies_files(tmp_path):
    """Test create_backup copies tracked files and creates manifest."""
    target_base = str(tmp_path / "target")
    os.makedirs(os.path.join(target_base, "plugins"))
    with open(os.path.join(target_base, "plugins", "test.conf"), "w") as f:
        f.write("content1")
    with open(os.path.join(target_base, "docker-compose.yml"), "w") as f:
        f.write("services: {}")

    backup_dir = create_backup(target_base, ["plugins/test.conf", "docker-compose.yml"])

    # Verify backup directory exists
    assert os.path.isdir(backup_dir)
    # Verify files copied
    assert os.path.exists(os.path.join(backup_dir, "plugins", "test.conf"))
    assert os.path.exists(os.path.join(backup_dir, "docker-compose.yml"))
    # Verify manifest
    with open(os.path.join(backup_dir, "manifest.json")) as f:
        manifest = json.load(f)
    assert "files" in manifest
    assert len(manifest["files"]) == 2
    # Verify latest symlink
    backup_root = os.path.join(target_base, ".backups")
    latest_link = os.path.join(backup_root, "latest")
    assert os.path.islink(latest_link)
    assert os.path.realpath(latest_link) == backup_dir


def test_create_backup_tracks_missing_files(tmp_path):
    """Test create_backup tracks files that don't exist yet (for creation)."""
    target_base = str(tmp_path / "target")
    os.makedirs(target_base)

    backup_dir = create_backup(target_base, ["plugins/new.conf"])

    with open(os.path.join(backup_dir, "manifest.json")) as f:
        manifest = json.load(f)
    file_info = manifest["files"][0]
    assert file_info["existed"] is False
    assert file_info["action"] == "created"
    # Marker file should exist
    marker = os.path.join(backup_dir, ".created_new.conf")
    assert os.path.exists(marker)


def test_restore_backup_restores_modified_files(tmp_path):
    """Test restore_backup restores files that existed in backup."""
    target_base = str(tmp_path / "target")
    os.makedirs(target_base)
    with open(os.path.join(target_base, "test.txt"), "w") as f:
        f.write("original")

    backup_dir = create_backup(target_base, ["test.txt"])
    # Modify the file
    with open(os.path.join(target_base, "test.txt"), "w") as f:
        f.write("modified")

    success = restore_backup(backup_dir)
    assert success is True
    with open(os.path.join(target_base, "test.txt")) as f:
        assert f.read() == "original"


def test_restore_backup_removes_created_files(tmp_path):
    """Test restore_backup deletes files that were created during install."""
    target_base = str(tmp_path / "target")
    os.makedirs(os.path.join(target_base, "plugins"))
    # Simulate: file didn't exist before install, was created
    backup_dir = create_backup(target_base, ["plugins/created.conf"])
    # File now exists in target (simulating install created it)
    with open(os.path.join(target_base, "plugins", "created.conf"), "w") as f:
        f.write("new file")

    success = restore_backup(backup_dir)
    assert success is True
    assert not os.path.exists(os.path.join(target_base, "plugins", "created.conf"))


def test_restore_backup_handles_missing_backup(tmp_path):
    """Test restore_backup returns False for non-existent backup."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    success = restore_backup(str(tmp_path / "nonexistent"))
    assert success is False


def test_find_latest_backup_returns_symlink_target(tmp_path):
    """Test find_latest_backup follows 'latest' symlink."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    backup_root = target_base / ".backups"
    backup_root.mkdir()
    # Create two backups
    old_dir = backup_root / "1000"
    new_dir = backup_root / "2000"
    old_dir.mkdir()
    new_dir.mkdir()
    for d in [old_dir, new_dir]:
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"files": []}, f)

    # Create latest symlink to newest
    os.symlink("2000", os.path.join(backup_root, "latest"))

    result = find_latest_backup(target_base)
    assert result == str(new_dir)


def test_find_latest_backup_fallback_to_newest_timestamp(tmp_path):
    """Test find_latest_backup returns newest timestamp when symlink broken."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    backup_root = target_base / ".backups"
    backup_root.mkdir()
    # Create backups without symlink
    for ts in ["1000", "2000", "3000"]:
        d = backup_root / ts
        d.mkdir()
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"files": []}, f)

    result = find_latest_backup(target_base)
    assert result.endswith("3000")  # newest


def test_find_oldest_backup_returns_earliest(tmp_path):
    """Test find_oldest_backup returns earliest timestamp."""
    target_base = str(tmp_path / "target")
    os.makedirs(target_base)
    backup_root = os.path.join(target_base, ".backups")
    os.makedirs(backup_root)
    for ts in ["3000", "1000", "2000"]:
        d = os.path.join(backup_root, ts)
        os.makedirs(d)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"files": []}, f)

    result = find_oldest_backup(target_base)
    assert result is not None
    assert result.endswith("1000")


def test_list_backups_prints_table(caplog, tmp_path):
    """Test list_backups outputs formatted table via logging."""

    caplog.set_level(logging.INFO)

    target_base = str(tmp_path / "target")
    os.makedirs(target_base)
    backup_root = os.path.join(target_base, ".backups")
    os.makedirs(backup_root)
    for ts in ["1000"]:
        d = os.path.join(backup_root, ts)
        os.makedirs(d)
        manifest_path = os.path.join(d, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump({"timestamp": ts, "created_at": "2024-01-01T00:00:00Z", "files": [{"path": "a", "existed": True}]}, f)

    list_backups(target_base)
    # Check that log contains expected output
    assert any("Available backups:" in rec.message for rec in caplog.records)
    assert any("1000" in rec.message for rec in caplog.records)


# === File Modification Tests ===


def test_update_start_xrproxy_rpc_config_idempotent(tmp_path):
    """Test that running update twice doesn't duplicate config."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    scripts_dir = target_base / "scripts"
    scripts_dir.mkdir()
    start_xrproxy = scripts_dir / "start-xrproxy.sh"

    # Create initial file with two EOL markers and existing plugins line
    initial_content = """#!/bin/bash
# Header
echo "EOL"
echo "EOL"""
    start_xrproxy.write_text(initial_content)

    initialize_paths(str(target_base))
    # First run
    modified1 = update_start_xrproxy_rpc_config(dry_run=False)
    assert modified1 is True
    content1 = start_xrproxy.read_text()
    # Second run should detect existing config
    modified2 = update_start_xrproxy_rpc_config(dry_run=False)
    assert modified2 is False
    content2 = start_xrproxy.read_text()
    # Content should be identical
    assert content1 == content2


def test_update_start_xrproxy_rpc_config_inserts_after_second_eol(tmp_path):
    """Test RPC config is inserted between the two EOL markers."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    scripts_dir = target_base / "scripts"
    scripts_dir.mkdir()
    start_xrproxy = scripts_dir / "start-xrproxy.sh"

    initial_content = """#!/bin/bash
echo "EOL"
echo "EOL"
PLUGINS=something"""
    start_xrproxy.write_text(initial_content)

    initialize_paths(str(target_base))
    update_start_xrproxy_rpc_config(dry_run=False)

    content = start_xrproxy.read_text()
    # Find positions
    first_eol = content.find("EOL")
    second_eol = content.find("EOL", first_eol + 1)
    # RPC config should appear between them
    between = content[first_eol + 3 : second_eol]
    assert "RPC_cg_coins_list_HOSTIP" in between
    assert "RPC_cg_coins_data_HOSTIP" in between


def test_modify_start_snode_plugins_inserts_after_wallets(tmp_path):
    """Test plugins line is inserted after wallets= in xrouter.conf section."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    scripts_dir = target_base / "scripts"
    scripts_dir.mkdir()
    start_snode = scripts_dir / "start-snode.sh"

    content = """#!/bin/bash
cat > /opt/blockchain/data/xrouter.conf << EOL
wallets=BTC,LTC
host=localhost
EOL"""
    start_snode.write_text(content)

    initialize_paths(str(target_base))
    modify_start_snode_plugins(dry_run=False)

    result = start_snode.read_text()
    # Should have plugins line after wallets
    assert "plugins=cg_coins_list,cg_coins_data" in result
    # Verify it's after wallets line
    wallets_idx = result.find("wallets=")
    plugins_idx = result.find("plugins=")
    assert wallets_idx < plugins_idx


def test_modify_docker_compose_adds_service(tmp_path):
    """Test docker-compose gets xr_service_cg_proxy service."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    docker_compose = target_base / "docker-compose.yml"

    initial = {"services": {"snode": {"image": "blocknet"}}}
    docker_compose.write_text(yaml.dump(initial))

    initialize_paths(str(target_base))
    modify_docker_compose(dry_run=False)

    result = yaml.safe_load(docker_compose.read_text())
    assert "xr_service_cg_proxy" in result["services"]
    svc = result["services"]["xr_service_cg_proxy"]
    assert svc["restart"] == "no"
    assert svc["stop_signal"] == "SIGINT"


def test_validate_target_base_rejects_missing_dir():
    """Test validate_target_base raises error for non-existent dir."""
    with pytest.raises(InstallerError, match="does not exist"):
        validate_target_base("/nonexistent/path")


def test_validate_target_base_rejects_no_docker_compose(tmp_path):
    """Test validate_target_base raises error without docker-compose.yml."""
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(InstallerError, match=r"docker-compose\.yml not found"):
        validate_target_base(str(target))


def test_validate_target_base_accepts_valid(tmp_path):
    """Test validate_target_base passes with valid directory."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "docker-compose.yml").write_text("services: {}")
    # Should not raise
    validate_target_base(str(target))


# === File Removal Test ===


def test_remove_plugin_files(tmp_path):
    """Test _remove_plugin_files deletes plugin configs if present."""
    target_base = tmp_path / "target"
    target_base.mkdir()
    plugins_dir = target_base / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "cg_coins_list.conf").write_text("test")
    (plugins_dir / "cg_coins_data.conf").write_text("test")
    (plugins_dir / "other.conf").write_text("other")

    _remove_plugin_files(str(target_base))
    assert not (plugins_dir / "cg_coins_list.conf").exists()
    assert not (plugins_dir / "cg_coins_data.conf").exists()
    assert (plugins_dir / "other.conf").exists()  # other files untouched


# === xrouter.conf Parsing Helpers ===


def test_find_xrouter_section_parses_correctly():
    """Test _find_xrouter_section locates heredoc boundaries."""
    lines = [
        "#!/bin/bash\n",
        "cat > /opt/blockchain/data/xrouter.conf << EOL\n",
        "wallets=BTC\n",
        "EOL\n",
    ]
    start, end = _find_xrouter_section(lines)
    assert start == 1
    assert end == 3


def test_find_xrouter_section_raises_if_missing():
    """Test _find_xrouter_section raises when section not found."""
    lines = ["#!/bin/bash\n", "echo hello\n"]
    with pytest.raises(InstallerError, match=r"xrouter\.conf section not found"):
        _find_xrouter_section(lines)


def test_find_plugins_line_returns_index_or_minus_one():
    """Test _find_plugins_line finds existing or returns -1."""
    lines = [
        "cat > xrouter.conf << EOL\n",
        "wallets=BTC\n",
        "plugins=existing\n",
        "EOL\n",
    ]
    idx = _find_plugins_line(lines, 0, 3)
    assert idx == 2

    lines2 = [
        "cat > xrouter.conf << EOL\n",
        "wallets=BTC\n",
        "EOL\n",
    ]
    idx2 = _find_plugins_line(lines2, 0, 2)
    assert idx2 == -1


def test_determine_insert_position_prefers_after_wallets():
    """Test _determine_insert_position chooses best insertion point."""
    lines = [
        "cat > xrouter.conf << EOL\n",
        "wallets=BTC\n",
        "host=localhost\n",
        "EOL\n",
    ]
    pos = _determine_insert_position(lines, 0, 3)
    # Should be after wallets (line 1), so index 2
    assert pos == 2


def test_determine_insert_position_falls_back_to_host():
    """Test falls back to host= when wallets= not present."""
    lines = [
        "cat > xrouter.conf << EOL\n",
        "host=localhost\n",
        "EOL\n",
    ]
    pos = _determine_insert_position(lines, 0, 2)
    assert pos == 2  # after line 1 (host)


def test_determine_insert_position_defaults_after_section_start():
    """Test defaults to after section start if no wallets or host."""
    lines = [
        "cat > xrouter.conf << EOL\n",
        "EOL\n",
    ]
    pos = _determine_insert_position(lines, 0, 1)
    assert pos == 1  # after line 0
