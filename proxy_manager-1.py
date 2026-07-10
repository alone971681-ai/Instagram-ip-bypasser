"""
proxy_manager.py — Auto Proxy Engine with SOCKS5 Support
==========================================================
Flow: FETCH all sources in parallel → TEST all candidates concurrently
      → SORT by latency → WORK (bot uses fastest proxies first)

SOCKS5 proxies are detected by source and tested with socks5:// scheme.
They tend to be faster and lower-latency for Discord's WebSocket gateway.
Pool is always sorted fastest-first so the best proxy is picked first.
"""

import os
import time
import random
import logging
import threading
import requests
from typing import Optional

log = logging.getLogger("proxy")

# ── Source definitions ─────────────────────────────────────────────────────────
# Each entry: (url, scheme)
# scheme tells us how to prefix candidates from that source.
# Sources are fetched in PARALLEL so all download simultaneously.
_SOURCES: list[tuple[str, str]] = [
    # ── ProxyScrape API (most reliable, updated every minute) ─────────────────
    (
        "https://api.proxyscrape.com/v3/free-proxy-list/get"
        "?request=displayproxies&protocol=socks5&timeout=5000&anonymity=elite&limit=300",
        "socks5",
    ),
    (
        "https://api.proxyscrape.com/v3/free-proxy-list/get"
        "?request=displayproxies&protocol=http&timeout=5000"
        "&country=US,GB,CA,NL,DE,FR&anonymity=elite&limit=300",
        "http",
    ),
    (
        "https://api.proxyscrape.com/v3/free-proxy-list/get"
        "?request=displayproxies&protocol=https&timeout=5000"
        "&anonymity=elite&limit=300",
        "http",
    ),
    # ── GitHub community lists (continuously maintained) ───────────────────────
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",   "socks5"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",     "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",   "http"),
    ("https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",     "socks5"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",    "socks5"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",     "http"),
    ("https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt", "http"),
    (
        "https://raw.githubusercontent.com/elliottophellia/yakumo/master"
        "/results/http/global/http_checked.txt",
        "http",
    ),
]

# ── Test settings ──────────────────────────────────────────────────────────────
# We hit Discord's actual gateway endpoint — if a proxy reaches this it works for the bot.
_DISCORD_TEST  = "https://discord.com/api/v9/gateway"
_TEST_TIMEOUT  = 7     # seconds per proxy test
_TEST_WORKERS  = 60    # concurrent test threads (more = faster pool build)

# ── Pool settings ──────────────────────────────────────────────────────────────
_POOL_SIZE    = 15    # confirmed-working proxies to keep (sorted fastest-first)
_REFRESH_SECS = 3600  # full rebuild every hour
_MIN_POOL     = 3     # emergency rebuild if pool drops below this

# ── Internal state ─────────────────────────────────────────────────────────────
# Pool entries: (latency_ms: float, proxy_url: str)
# Sorted ascending so pool[0] is always the fastest.
_pool:      list[tuple[float, str]] = []
_pool_lock: threading.Lock = threading.Lock()
_last_refresh:   float = 0.0
_refresh_running: bool = False


# ── Step 1: FETCH ──────────────────────────────────────────────────────────────

def _fetch_source(url: str, scheme: str) -> list[tuple[str, str]]:
    """
    Download one source URL and return tagged candidates: [(scheme, 'host:port'), ...]
    Runs in its own thread so all sources are fetched simultaneously.
    """
    results = []
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return results
        for line in r.text.splitlines():
            line = line.strip()
            # Accept bare host:port lines only
            if ":" in line and " " not in line and len(line) < 50:
                results.append((scheme, line))
    except Exception:
        pass
    return results


def _fetch_all() -> list[tuple[str, str]]:
    """
    Fetch ALL sources in parallel threads.
    Returns deduplicated list of (scheme, 'host:port'), SOCKS5 first.
    """
    bucket: list[list[tuple[str, str]]] = [[] for _ in _SOURCES]
    threads = []

    def _worker(idx: int, url: str, scheme: str):
        bucket[idx] = _fetch_source(url, scheme)

    for i, (url, scheme) in enumerate(_SOURCES):
        t = threading.Thread(target=_worker, args=(i, url, scheme), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=20)

    # Merge and deduplicate (addr is unique key regardless of scheme)
    seen: set[str] = set()
    socks5: list[tuple[str, str]] = []
    http:   list[tuple[str, str]] = []

    for bucket_items in bucket:
        for scheme, addr in bucket_items:
            if addr not in seen:
                seen.add(addr)
                if scheme == "socks5":
                    socks5.append((scheme, addr))
                else:
                    http.append((scheme, addr))

    # Randomise within each group so we don't always hit the same IPs
    random.shuffle(socks5)
    random.shuffle(http)

    # SOCKS5 first — they're faster for Discord's WebSocket gateway
    candidates = socks5 + http
    log.info(
        f"[proxy] Fetched {len(candidates)} candidates "
        f"({len(socks5)} SOCKS5, {len(http)} HTTP) from {len(_SOURCES)} sources"
    )
    return candidates


# ── Step 2: TEST ───────────────────────────────────────────────────────────────

def _test_proxy(scheme: str, addr: str) -> Optional[tuple[float, str]]:
    """
    Test one proxy against Discord's gateway endpoint.
    Returns (latency_ms, proxy_url) if it works, None if it doesn't.
    SOCKS5 uses socks5:// scheme; HTTP uses http://.
    """
    proxy_url = f"{scheme}://{addr}"
    try:
        t0 = time.monotonic()
        r = requests.get(
            _DISCORD_TEST,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=_TEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        latency = (time.monotonic() - t0) * 1000  # ms
        if r.status_code == 200 and "url" in r.text:
            return (latency, proxy_url)
    except Exception:
        pass
    return None


def _test_batch(
    candidates: list[tuple[str, str]],
    target: int,
    results: list[tuple[float, str]],
    results_lock: threading.Lock,
) -> None:
    """
    Test candidates using a thread pool of _TEST_WORKERS workers.
    Stops early once `target` working proxies are found.
    """
    queue = list(candidates)   # copy so we can pop safely
    q_lock = threading.Lock()

    def _worker():
        while True:
            with q_lock:
                if not queue:
                    return
                with results_lock:
                    if len(results) >= target:
                        return
                scheme, addr = queue.pop(0)

            result = _test_proxy(scheme, addr)

            if result:
                with results_lock:
                    results.append(result)
                    count = len(results)
                if count <= target:
                    lat, url = result
                    ptype = "SOCKS5" if url.startswith("socks5") else "HTTP"
                    log.info(
                        f"[proxy] #{count:>2} found  {ptype:<6}  "
                        f"{lat:>5.0f}ms  {url}"
                    )

    threads = [
        threading.Thread(target=_worker, daemon=True)
        for _ in range(min(_TEST_WORKERS, len(queue)))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ── Step 3: BUILD POOL ─────────────────────────────────────────────────────────

def _build_pool() -> None:
    """
    Full rebuild: FETCH → TEST → SORT by latency → update pool.
    Runs in a background daemon thread so it never blocks the bot.
    """
    global _last_refresh, _refresh_running

    _refresh_running = True
    log.info("[proxy] ── Pool rebuild started ──────────────────────")

    # FETCH
    candidates = _fetch_all()

    # TEST
    results: list[tuple[float, str]] = []
    results_lock = threading.Lock()
    log.info(f"[proxy] Testing with {_TEST_WORKERS} concurrent workers…")
    _test_batch(candidates, _POOL_SIZE, results, results_lock)

    # SORT fastest-first, cap to pool size
    results.sort(key=lambda x: x[0])
    best = results[:_POOL_SIZE]

    # UPDATE pool atomically
    with _pool_lock:
        _pool.clear()
        _pool.extend(best)

    _last_refresh    = time.time()
    _refresh_running = False

    if _pool:
        s5 = sum(1 for _, u in _pool if u.startswith("socks5"))
        ht = len(_pool) - s5
        avg = sum(l for l, _ in _pool) / len(_pool)
        log.info(
            f"[proxy] ✅ Pool ready — {len(_pool)} proxies  "
            f"({s5} SOCKS5, {ht} HTTP)  avg {avg:.0f}ms"
        )
        log.info("[proxy] Fastest proxies:")
        for lat, url in _pool[:5]:
            ptype = "SOCKS5" if url.startswith("socks5") else "HTTP  "
            log.info(f"[proxy]   {ptype}  {lat:>5.0f}ms  {url}")
    else:
        log.warning("[proxy] ⚠️  No working proxies found — will retry shortly")
        # Schedule a quick retry in 5 minutes instead of waiting a full hour
        threading.Timer(300, _start_refresh_thread).start()


def _start_refresh_thread() -> None:
    global _refresh_running
    if _refresh_running:
        return
    t = threading.Thread(target=_build_pool, daemon=True)
    t.start()


def _maybe_refresh() -> None:
    """Trigger background rebuild if pool is stale or too small."""
    with _pool_lock:
        size = len(_pool)
    stale = (time.time() - _last_refresh) > _REFRESH_SECS
    small = size < _MIN_POOL
    if (stale or small) and not _refresh_running:
        _start_refresh_thread()


# ── Public API ─────────────────────────────────────────────────────────────────

def start() -> None:
    """
    Call once at startup. Begins parallel fetch + test in the background.
    Returns immediately — pool becomes available within ~30-60 seconds.
    If PROXY_URL is set in env, skips auto-discovery entirely.
    """
    if os.environ.get("PROXY_URL"):
        log.info("[proxy] PROXY_URL is set — using that, skipping auto-discovery.")
        return
    log.info("[proxy] Starting auto-proxy discovery (SOCKS5 + HTTP)…")
    _start_refresh_thread()


def get_proxy() -> Optional[str]:
    """
    Return the fastest confirmed-working proxy URL, or None if pool is empty.
    Always picks from the front of the latency-sorted pool (fastest first).
    Also triggers a background refresh if pool is stale or too small.
    """
    manual = os.environ.get("PROXY_URL", "").strip()
    if manual:
        return manual

    _maybe_refresh()

    with _pool_lock:
        if not _pool:
            return None
        # Weighted pick: bias strongly toward the fastest proxies
        # Use the top-3 80% of the time, rest of pool 20%
        top = _pool[:3]
        if top and random.random() < 0.80:
            return random.choice(top)[1]
        return random.choice(_pool)[1]


def remove_proxy(proxy_url: str) -> None:
    """
    Evict a proxy that failed a live request.
    Triggers an emergency rebuild if the pool drops below _MIN_POOL.
    """
    with _pool_lock:
        before = len(_pool)
        _pool[:] = [(l, u) for l, u in _pool if u != proxy_url]
        after = len(_pool)
    if before != after:
        ptype = "SOCKS5" if proxy_url.startswith("socks5") else "HTTP"
        log.warning(
            f"[proxy] Evicted dead {ptype}: {proxy_url}  ({after} remaining)"
        )
    _maybe_refresh()


def pool_status() -> dict:
    """Return a snapshot of the current pool for the dashboard."""
    with _pool_lock:
        size   = len(_pool)
        sample = [u for _, u in _pool[:5]]
        avg_ms = (sum(l for l, _ in _pool) / size) if size else 0
        s5     = sum(1 for _, u in _pool if u.startswith("socks5"))

    return {
        "pool_size":    size,
        "socks5_count": s5,
        "http_count":   size - s5,
        "avg_latency":  round(avg_ms),
        "refreshing":   _refresh_running,
        "last_refresh": _last_refresh,
        "sample":       sample,
        "manual":       bool(os.environ.get("PROXY_URL")),
    }
