"""
Coingecko Caching Proxy Service for XRouter/XCloud

Provides two JSON-RPC methods:
- cg_coins_list: Returns list of all supported coin tokens and IDs
- cg_coins_data: Returns cached price data for requested token IDs

Features:
- Asynchronous HTTP client with connection pooling
- In-memory LRU cache with size limits and TTL
- Health check endpoint
- Graceful shutdown
- Configurable logging
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections import OrderedDict
from typing import Any, TypedDict

from aiohttp import ClientSession, ClientTimeout, web


# === Logging Configuration ===
def setup_logging(log_file: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger with console and optional file handler."""
    logger_obj = logging.getLogger("cg_proxy")
    logger_obj.setLevel(level)
    logger_obj.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger_obj.addHandler(console_handler)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger_obj.addHandler(file_handler)
            logger_obj.info(f"Logging to file: {log_file}")
        except Exception as e:
            logger_obj.error(f"Failed to setup file logging: {e}")

    return logger_obj


# === Configuration ===
CACHE_TTL = 3600  # Cache entries expire after 1 hour
MAX_CACHE_ITEMS = 20000  # Maximum number of price entries to store
COINGECKO_API = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT = 30  # Seconds for external API calls
CG_RATE_DELAY = 30  # Seconds between CoinGecko API calls (rate limiting)
COINS_LIST_INTERVAL = 3600  # Refresh coins list every hour
MAX_URL_LENGTH = 8000  # Max URL length for CoinGecko requests
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080
LOG_FILE = os.getenv("CG_PROXY_LOG_FILE")

logger = setup_logging(log_file=LOG_FILE, level=logging.INFO)


# === Type Definitions ===
class CoinGeckoCoin(TypedDict):
    """Coin data from CoinGecko coins/list endpoint."""

    id: str
    symbol: str
    name: str


class CoinGeckoPrice(TypedDict):
    """Price data from CoinGecko simple/price endpoint."""

    usd: float
    usd_24h_vol: float
    timestamp: float


class CacheEntry(TypedDict):
    """Internal cache entry structure."""

    value: Any
    timestamp: float


# === Cache Management ===
class LRUCache:
    """Async LRU cache with TTL eviction. Each instance has its own lock."""

    def __init__(self, maxsize: int = 10000, ttl: int = 3600) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()  # key -> (value, timestamp)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, timestamp = entry
            if time.time() - timestamp > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (value, time.time())
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # Evict least recently used

    def __len__(self) -> int:
        return len(self._data)


# === Global State ===
price_cache: LRUCache = LRUCache(maxsize=MAX_CACHE_ITEMS, ttl=CACHE_TTL)
coins_list_cache: dict[str, Any] = {"data": None, "timestamp": 0}
coins_list_lock: asyncio.Lock = asyncio.Lock()  # Dedicated lock for coins_list_cache

shutdown_flag: asyncio.Event = asyncio.Event()
http_session: ClientSession | None = None
start_time: float = 0


# === Utility ===
async def sleep_interruptible(duration: float) -> None:
    """Sleep for `duration` seconds, waking early if shutdown is requested."""
    try:
        await asyncio.wait_for(shutdown_flag.wait(), timeout=duration)
    except asyncio.TimeoutError:
        pass  # Normal path: full sleep elapsed, no shutdown requested
    except asyncio.CancelledError:
        raise


# === CoinGecko API Client ===
async def fetch_json(session: ClientSession, url: str) -> Any | None:
    """Fetch and parse JSON from a URL, with retries on failure."""
    for attempt in range(1, 6):
        try:
            logger.debug(f"GET {url} (attempt {attempt})")
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"HTTP {resp.status} from {url}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on attempt {attempt} fetching {url}")
        except Exception as e:
            logger.error(f"Error on attempt {attempt} fetching {url}: {e}")

        if attempt < 5:
            await sleep_interruptible(30 * attempt)

    logger.error(f"All attempts failed for {url}")
    return None


# === Background Tasks ===
async def update_coingecko_coins_list(session: ClientSession) -> None:
    """Periodically fetch and cache the complete CoinGecko coins list."""
    logger.info("Started coins list updater task")
    while not shutdown_flag.is_set():
        try:
            data = await fetch_json(session, f"{COINGECKO_API}/coins/list")
            if data:
                # FIX: Use the dedicated coins_list_lock, not price_cache's lock.
                #      Never mix locks across unrelated data structures.
                async with coins_list_lock:
                    coins_list_cache["data"] = data
                    coins_list_cache["timestamp"] = time.time()
                logger.info(f"Updated coins list: {len(data)} tokens")
        except asyncio.CancelledError:
            logger.info("Coins list updater cancelled")
            break
        except Exception as e:
            logger.error(f"Failed to update coins list: {e}")

        await sleep_interruptible(COINS_LIST_INTERVAL)


def _build_coin_chunks(coin_ids: list[str], base_url: str, max_url_length: int) -> list[list[str]]:
    """
    Build chunks of coin IDs that fit within URL length limit.
    Returns a list of chunks (each chunk is a list of coin IDs).
    """
    chunks: list[list[str]] = []
    i = 0
    n = len(coin_ids)
    while i < n:
        chunk: list[str] = []
        for j in range(i, n):
            candidate = [*chunk, coin_ids[j]]
            if len(base_url.format(ids=",".join(candidate))) > max_url_length:
                break
            chunk = candidate
        if not chunk:
            # Single ID exceeds limit - skip it
            logger.warning(f"Skipping oversized coin ID: {coin_ids[i]}")
            i += 1
            continue
        chunks.append(chunk)
        i += len(chunk)
    return chunks


async def update_coingecko_coins_tickers(session: ClientSession) -> None:
    """
    Periodically fetch USD price data for all known coins in URL-sized chunks.
    Respects CoinGecko rate limits with a configurable delay between requests.
    """
    logger.info("Started coins tickers updater task")
    base_url = f"{COINGECKO_API}/simple/price?ids={{ids}}&vs_currencies=usd&include_24hr_vol=true"
    while not shutdown_flag.is_set():
        try:
            async with coins_list_lock:
                coins_data = coins_list_cache.get("data")

            if not coins_data:
                logger.info("Coins list not ready yet, retrying in 60s...")
                await sleep_interruptible(60)
                continue

            coin_ids = [entry["id"] for entry in coins_data]
            logger.info(f"Starting ticker refresh for {len(coin_ids)} coins")

            chunks = _build_coin_chunks(coin_ids, base_url, MAX_URL_LENGTH)
            count = 0
            for chunk in chunks:
                if shutdown_flag.is_set():
                    break
                url = base_url.format(ids=",".join(chunk))
                data = await fetch_json(session, url)
                if data:
                    timestamp = time.time()
                    for key, val in data.items():
                        val["timestamp"] = timestamp
                        await price_cache.set(key, val)
                    logger.debug(f"Updated {len(data)} price entries (chunk of {len(chunk)})")
                logger.info(f"Updated chunk {count + 1} of total {len(chunks) + 1}")
                await sleep_interruptible(CG_RATE_DELAY)
                count += 1

            logger.info("Ticker refresh cycle complete")

        except asyncio.CancelledError:
            logger.info("Ticker updater cancelled")
            break
        except Exception as e:
            logger.error(f"Ticker update error: {e}", exc_info=True)
            await sleep_interruptible(15)


# === JSON-RPC Handlers ===
async def cg_coins_list_handler() -> dict[str, Any]:
    """Return the cached coins list."""
    async with coins_list_lock:
        data: list[CoinGeckoCoin] | None = coins_list_cache.get("data")
        timestamp = coins_list_cache.get("timestamp", 0)

    if data is not None:
        return {"success": True, "reply": {"data": data, "timestamp": timestamp}}
    return {"success": False, "reply": "Coins list not available yet, try again shortly"}


async def cg_coins_data_handler(params: list[str]) -> dict[str, Any]:
    """Return cached price data for the requested coin IDs."""
    if not params:
        return {"success": False, "reply": "No coin IDs provided"}

    results: dict[str, Any] = {}
    for coin_id in params:
        # Coerce to string to handle non-string inputs gracefully
        coin_id_str = str(coin_id)
        cached = await price_cache.get(coin_id_str.lower())
        if cached is not None:
            results[coin_id] = cached
        else:
            results[coin_id] = {"code": 404, "error": "coin not in cache"}

    # Preserve original semantics: success=False when nothing was found at all,
    # so clients relying on the top-level flag are not broken.
    if any(isinstance(v, dict) and v.get("code") != 404 for v in results.values()):
        return {"success": True, "reply": results}
    return {"success": False, "reply": results}


# === Health Check ===
async def health_handler(_request: web.Request) -> web.Response:
    async with coins_list_lock:
        data = coins_list_cache.get("data")
        coins_available = data is not None
        coins_count = len(data) if data else 0

    return web.json_response(
        {
            "status": "shutting_down" if shutdown_flag.is_set() else "healthy",
            "cache_size": len(price_cache),
            "coins_list_available": coins_available,
            "coins_list_count": coins_count,
            "uptime_seconds": round(time.time() - start_time, 1),
        }
    )


# === Web Server ===
async def handle_request(request: web.Request) -> web.Response:
    """Main JSON-RPC request router."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "reply": "Invalid JSON in request body"}, status=400)

    method = body.get("method")
    params = body.get("params", [])
    client_ip = request.headers.get("X-Forwarded-For", request.remote)
    logger.info(f"Request from {client_ip}: method={method}")

    try:
        if method == "cg_coins_list":
            result = await cg_coins_list_handler()
        elif method == "cg_coins_data":
            # Type check params - should be list of strings
            if not isinstance(params, list):
                return web.json_response({"success": False, "reply": "Invalid params: must be a list"}, status=400)
            result = await cg_coins_data_handler(params)
        else:
            logger.warning(f"Unknown method requested: {method!r}")
            return web.json_response({"success": False, "reply": f"Unknown method: {method!r}"}, status=400)
    except Exception as e:
        logger.error(f"Handler error for method={method}: {e}", exc_info=True)
        return web.json_response({"success": False, "reply": f"Internal server error: {e}"}, status=500)

    return web.json_response(result)


async def start_server() -> None:
    """Configure and start the aiohttp web server."""
    app = web.Application()
    app.router.add_post("/", handle_request)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)

    try:
        await site.start()
    except Exception as e:
        logger.error(f"Failed to start server on {SERVER_HOST}:{SERVER_PORT}: {e}", exc_info=True)
        await runner.cleanup()
        shutdown_flag.set()
        return

    logger.info(f"JSON-RPC server listening on http://{SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Health check at http://{SERVER_HOST}:{SERVER_PORT}/health")

    try:
        await shutdown_flag.wait()
    finally:
        await runner.cleanup()
        logger.debug("Server runner cleanup completed")


# === Signal Handling & Shutdown ===
def setup_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGTERM and SIGINT handlers to trigger graceful shutdown."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))


async def shutdown() -> None:
    """Graceful shutdown: signal tasks to stop."""
    if shutdown_flag.is_set():
        return
    logger.info("Shutdown signal received, stopping...")
    shutdown_flag.set()
    logger.info("Shutdown complete")


# === Entry Point ===
async def main() -> None:
    """Main entry point: initialize resources, start background tasks, await shutdown."""
    global start_time, http_session
    start_time = time.time()
    logger.info("Starting Coingecko Proxy Service")

    # Initialize HTTP session at startup (avoids race condition)
    http_session = ClientSession(timeout=ClientTimeout(total=REQUEST_TIMEOUT))

    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(update_coingecko_coins_list(http_session), name="coins_list_updater"),
        asyncio.create_task(update_coingecko_coins_tickers(http_session), name="tickers_updater"),
        asyncio.create_task(start_server(), name="web_server"),
    ]

    try:
        await shutdown_flag.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if http_session and not http_session.closed:
            await http_session.close()
            logger.info("HTTP session closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
