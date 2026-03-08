# Coingecko Caching Proxy for XRouter/XCloud

## Overview

This service caches CoinGecko token list and price data. It provides two XRouter plugins (`cg_coins_list`, `cg_coins_data`) via a Docker container that runs alongside your existing `exrproxy-env` setup.

## Quick Start

```bash
# Clone repository (or use existing)
cd ~/exrproxy-env
git clone https://github.com/tryiou/xr_services
cd xr_services
pip3 install -r requirements.txt

# Install and start the service
./cgproxy-ctl install --deploy

# Check status
./cgproxy-ctl status

# View logs
./cgproxy-ctl logs --follow
```

## Prerequisites

- Existing `exrproxy-env` service node setup
- Python 3.6+ (for `cgproxy-ctl`)
- Docker & docker-compose
- `rsync` (used for backup/restore)
- Python dependencies: `pip3 install -r requirements.txt`

## Installation

### Step-by-Step

1. **Clone the repository** into any location (not necessarily inside `exrproxy-env`):
   ```bash
   git clone https://github.com/tryiou/xr_services ~/xr_services
   cd ~/xr_services
   pip3 install -r requirements.txt
   ```

2. **Install configuration files**:
   ```bash
   ./cgproxy-ctl install [--target-dir PATH]
   ```
   If `--target-dir` is omitted, the script will auto-detect `~/exrproxy-env`.

   The installer will:
   - Create an automatic backup in `~/exrproxy-env/.backups/<timestamp>/`
   - Copy plugin configurations to `~/exrproxy-env/plugins/`
   - Update `start-xrproxy.sh` with RPC endpoints
   - Update `start-snode.sh` to enable plugins in `xrouter.conf`
   - Add `xr_service_cg_proxy` service to `docker-compose.yml`

3. **Start the service**:
   ```bash
   ./cgproxy-ctl deploy
   ```

   Or combine steps 2 and 3:
   ```bash
   ./cgproxy-ctl install --deploy
   ```

4. **Verify installation**:
   ```bash
   # Check service status and health
   ./cgproxy-ctl status

   # View logs (container's internal port 8080 is not exposed to host)
   ./cgproxy-ctl logs --follow

   # Wait for cache warmup (1-2 hours), then test via XRouter
   curl -X POST http://your-node.com/xrs/cg_coins_list \
     -H 'Content-Type: application/json' \
     -d '{}'
   ```

### Installer Options

| Flag | Description |
|------|-------------|
| `--target-dir PATH` | Target exrproxy-env (default: auto-detect from `EXRPROXY_ENV` or `~/exrproxy-env`) |
| `--dry-run` | Preview changes without modifying files |
| `--no-backup` | Skip automatic backup (use with caution) |
| `--deploy` | Also start the container after install (install command only) |
| `--force` | Skip confirmation prompts |

**Note:** The Docker build context uses an absolute path to the repository, so you can run `cgproxy-ctl` from any location without symlinks or copying files.

## Configuration

### Plugin Files (`~/exrproxy-env/plugins/*.conf`)

The plugins are configured via `.conf` files:

```
parameters=string      # Expected parameter type (string, array, etc.)
fee=0                 # Fee in BLOCK per call (0 = free)
clientrequestlimit=50 # Maximum requests per client
disabled=0            # 0=enabled, 1=disabled
help=Usage string...  # Help text shown to users
```

After editing these files, rebuild/restart the container:
```bash
./cgproxy-ctl rebuild
```

### Service Constants

To modify cache TTL, rate limits, or other runtime parameters, edit the constants in `cg_proxy_xrs.py` and rebuild the Docker image:

```bash
./cgproxy-ctl rebuild
```

Key constants:
- `CACHE_TTL = 3600` (1 hour)
- `MAX_CACHE_ITEMS = 10000`
- `CG_RATE_DELAY = 15` (seconds between CoinGecko API batches)
- `COINS_LIST_INTERVAL = 3600`

### RPC Credentials

The installer adds placeholder RPC credentials (`USER=A`, `PASS=B`) to `start-xrproxy.sh`. These satisfy xrproxy's required configuration format but are not used for authentication by this service. You may change them if desired; they have no functional impact on the proxy.

## Usage

### Through XRouter (Production)

Once installed and configured, your XRouter clients can call the methods directly:

```bash
# Get complete token list
curl -X POST http://your-node.com/xrs/cg_coins_list \
  -H 'Content-Type: application/json' \
  -d '{}'

# Get price data for specific tokens
curl -X POST http://your-node.com/xrs/cg_coins_data \
  -H 'Content-Type: application/json' \
  -d '["bitcoin","ethereum","blocknet"]'
```

### Direct HTTP (Debug & Health)

```bash
# Health check endpoint (from inside Docker network)
docker-compose -f ~/exrproxy-env/docker-compose.yml exec xr_service_cg_proxy curl http://localhost:8080/health

# Direct RPC call (bypasses XRouter)
docker-compose -f ~/exrproxy-env/docker-compose.yml exec xr_service_cg_proxy \
  curl -X POST http://localhost:8080/ \
    -H 'Content-Type: application/json' \
    -d '{"method":"cg_coins_data","params":["bitcoin"]}'
```

**Health response:**
```json
{
  "status": "healthy",
  "cache_size": 8452,
  "coins_list_available": true,
  "uptime": 1234.56
}
```

## Command Reference

| Command | Description |
|---------|-------------|
| `install` | Install/configure the service (does not start container) |
| `uninstall` | Restore from backup, remove plugin files, stop container |
| `deploy` | Start the Docker container (`docker-compose up -d`) |
| `undeploy` | Stop and remove the container |
| `restart` | Restart the service |
| `status` | Show container status and health check |
| `logs [--follow]` | Show container logs (default: last 100 lines) |
| `backup [TAG]` | Create backup with optional human-readable tag |
| `restore ID` | Restore from backup by timestamp or tag |
| `list-backups` | List all available backups with tags |
| `check` | Run comprehensive diagnostics |
| `test [--skip-clean]` | Run automated test suite (developer mode) |
| `update` | Pull git changes, reinstall if service files changed |
| `shell` | Open interactive shell inside container |
| `exec CMD [ARGS...]` | Execute command inside container |

### Global Flags

- `--target-dir PATH` - Specify target exrproxy-env (overrides auto-detect)
- `--verbose, -v` - Enable debug logging
- `--dry-run` - Preview changes (for install/uninstall/backup)
- `--force` - Skip confirmation prompts

### Examples

```bash
# First-time install and start
./cgproxy-ctl install --deploy

# Create a tagged backup before making changes
./cgproxy-ctl backup "before-customization"

# Check service health
./cgproxy-ctl status

# View recent logs
./cgproxy-ctl logs --follow

# Restore from a specific backup
./cgproxy-ctl restore "pre-update"

# Update from git and reinstall if needed
./cgproxy-ctl update

# Run diagnostics
./cgproxy-ctl check

# Uninstall completely
./cgproxy-ctl uninstall
```

## Backup & Restore

- **Automatic backup** is created before any modification in `~/exrproxy-env/.backups/<timestamp>/`
- **On failure**, the installer automatically restores from the backup
- **Backups are kept** after successful install (safe to delete manually)
- **Tagging**: Use `./cgproxy-ctl backup "my-label"` to add a human-readable tag
- **List backups**: `./cgproxy-ctl list-backups`
- **Restore by tag**: `./cgproxy-ctl restore "my-label"`
- **Manual restore** (if needed):
  ```bash
  cp ~/exrproxy-env/.backups/latest/scripts/*.sh ~/exrproxy-env/scripts/
  cp ~/exrproxy-env/.backups/latest/docker-compose.yml ~/exrproxy-env/
  rm -f ~/exrproxy-env/plugins/cg_coins_*.conf
  ./cgproxy-ctl undeploy 2>/dev/null || true
  ```

## Uninstall

### Automatic (Recommended)

```bash
./cgproxy-ctl uninstall
```

This will:
- Restore all modified files from the oldest backup (pre-first-install state)
- Remove the plugin configuration files (`cg_coins_*.conf`)
- Stop and remove the Docker container
- Keep the backup directory for your records

### Manual (if backup is missing)

1. Remove plugin configs:
   ```bash
   rm -f ~/exrproxy-env/plugins/cg_coins_*.conf
   ```

2. Revert `start-xrproxy.sh`:
   - Delete the `set-ph = RPC_cg_coins_*` blocks added by installer
   - Remove `cg_coins_list,cg_coins_data` from the `set-ph = PLUGINS=` line

3. Revert `start-snode.sh`:
   - In the `xrouter.conf` heredoc section, delete the `plugins=cg_coins_list,cg_coins_data` line

4. Remove service from `docker-compose.yml`:
   - Delete the entire `xr_service_cg_proxy:` service block

5. Stop and remove the container:
   ```bash
   ./cgproxy-ctl undeploy
   ```

6. Optionally remove Docker image:
   ```bash
   docker rmi xr_services_xr_service_cg_proxy
   ```

## Troubleshooting

### "No data to serve" or empty responses

The cache needs 15-30min to warm up on first run. Check logs:
```bash
./cgproxy-ctl logs --follow
```

### "coin not in cache"

The token ID is not in the cache. First call `cg_coins_list` to see all available IDs, then request specific ones.

### Plugin not appearing in XRouter

1. Verify `plugins=cg_coins_list,cg_coins_data` is present in the `xrouter.conf` section of `start-snode.sh`
2. Ensure `start-xrproxy.sh` contains the `RPC_cg_coins_*` entries
3. Restart both `snode` and `xr_proxy` containers

### Port 8080 already in use

The service only needs to be accessible within the Docker network. XRouter connects via `xr_service_cg_proxy:8080`. If another Docker service uses this internal port, edit `docker-compose.yml` to change the exposed port.

### Permission errors

Run the installer as the same user that owns `~/exrproxy-env/`. Ensure write permissions on `plugins/`, `scripts/`, and `docker-compose.yml`.

### Restore from backup

```bash
./cgproxy-ctl restore <timestamp>
# Then reinstall if needed: ./cgproxy-ctl install
```

## Technical Details

### Architecture

```
Client → XRouter (port 80) → xr_service_cg_proxy:8080 → CoinGecko API
           ↑                      ↑
    start-xrproxy.sh        RPC config entries
    start-snode.sh          plugins= line
```

### Cache Strategy

- **Coins list**: Fetched hourly (complete ~10,000 token list from CoinGecko)
- **Prices**: Fetched in chunks (URL length limited) with 15s delay between batches
- **TTL**: 1 hour per entry; LRU eviction when cache exceeds 10,000 items

### External Dependencies

- **CoinGecko API**: `https://api.coingecko.com/api/v3` (no API key required)
- **Rate limits**: Free tier allows ~50 calls/minute. Our 15s delay (~4 calls/minute) stays well within limits.

### Data Flow

1. Service starts, begins fetching coins list immediately
2. Once coins list is cached, price fetcher begins chunking requests for all known tokens
3. Cache fills progressively; expect 1-2 hours for full warmup
4. Clients receive cached data with timestamps; data refreshes on TTL cycle

### Build Context

The Docker build context uses an absolute path to the repository directory, allowing you to run `cgproxy-ctl` from any location without needing to clone the repository inside `exrproxy-env`.

## Multi-Node Deployment

Use `--target-dir` to deploy the same repository to multiple nodes:

```bash
./cgproxy-ctl install --target-dir ~/node1/exrproxy-env --deploy
./cgproxy-ctl install --target-dir ~/node2/exrproxy-env --deploy
```

## Support

- Issues: https://github.com/tryiou/xr_services/issues
