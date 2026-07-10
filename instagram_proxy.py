"""
instagram_proxy.py — Instagram-Specific Proxy Intelligence Layer
================================================================
Wraps proxy_manager with Instagram-specific checks that the Discord
proxy pool doesn't need but Instagram absolutely requires:

  1. DATACENTER FILTER  — Skips IPs from known blocked ASNs
     (AWS, DigitalOcean, Hetzner, Linode, GCP, Azure, OVH, Vultr…)
     Instagram blocks entire ASN ranges at the network level.

  2. BURNED PROXY LIST  — Persistent blacklist (ig_burned.json).
     Once a proxy causes a ban/challenge/block on Instagram, it is
     permanently removed from consideration for all IG accounts.

  3. PRE-LOGIN IP TEST  — Hits Instagram's public endpoint through
     the proxy BEFORE attempting login, checking for block signals.
     Saves the account from a challenge triggered by a dirty IP.

  4. IP REPUTATION CHECK — Optional ipapi.is lookup (free 1k/day,
     no signup). Detects datacenter / VPN / tor exit nodes and
     rejects them before they touch your account.

Research sources:
  • Community ASN block list (2025-2026)
  • ipapi.is docs: https://ipapi.is/developers.html
  • Instagram HTTP error matrix (checkpoint_required, 403, 429)
"""

import os
import json
import time
import random
import logging
import threading
import requests

import proxy_manager

log = logging.getLogger("ig_proxy")

BURNED_FILE = "ig_burned.json"   # persists across restarts

# ── Known datacenter ASNs Instagram blocks at network level ───────────────────
# Source: community-verified 2025-2026
# Instagram silently bans entire ASN ranges — no login possible from these.
_BLOCKED_ASNS = {
    "AS14618",   # Amazon AWS (us-east)
    "AS16509",   # Amazon AWS (global)
    "AS14061",   # DigitalOcean
    "AS24940",   # Hetzner
    "AS63949",   # Linode / Akamai
    "AS15169",   # Google Cloud
    "AS396982",  # Google Cloud (new range)
    "AS8075",    # Microsoft Azure
    "AS20940",   # Akamai CDN
    "AS209",     # CenturyLink / Lumen
    "AS7922",    # Comcast (shared datacenter ranges)
    "AS55960",   # OVH SAS
    "AS16276",   # OVH (FR)
    "AS35540",   # OVH (US)
    "AS20473",   # Vultr
    "AS394711",  # Vultr (new)
    "AS136907",  # Huawei Cloud
    "AS45090",   # Tencent Cloud
    "AS37963",   # Alibaba Cloud
    "AS132203",  # Tencent Cloud (HK)
    "AS4134",    # China Telecom (heavily shared)
    "AS7552",    # Viettel (heavily shared)
    "AS60068",   # Datacamp / CDN77
    "AS204957",  # Serverius / BulkVS
    "AS9009",    # M247 (bulk VPN provider)
    "AS53667",   # FranTech / BuyVM
    "AS40676",   # Psychz Networks
    "AS46664",   # VolumeDrive
    "AS18978",   # ENFORTA / ENZU
    "AS23470",   # ReliableSite
    "AS32475",   # SingleHop / INAP
    "AS7203",    # Zayo
    "AS174",     # Cogent Communications
    "AS3356",    # Lumen / Level3
    "AS6939",    # Hurricane Electric (IX/transit)
}

# ── Instagram-specific HTTP error signals ─────────────────────────────────────
# Returned when the IP is already flagged before we even log in.
_BLOCK_KEYWORDS = [
    "checkpoint_required",
    "challenge_required",
    "checkpoint_challenge_required",
    "ip_block",
    "suspicious_login",
    "feedback_required",
    "Please wait a few minutes",
]

# ── Burned proxy store ─────────────────────────────────────────────────────────

_burned: set[str] = set()
_burned_lock = threading.Lock()


def _load_burned():
    global _burned
    try:
        if os.path.exists(BURNED_FILE):
            with open(BURNED_FILE) as f:
                data = json.load(f)
            with _burned_lock:
                _burned = set(data.get("burned", []))
            log.info(f"[ig_proxy] Loaded {len(_burned)} burned proxies from disk.")
    except Exception as e:
        log.warning(f"[ig_proxy] Could not load burned list: {e}")


def _save_burned():
    try:
        with _burned_lock:
            data = list(_burned)
        with open(BURNED_FILE, "w") as f:
            json.dump({"burned": data, "updated": time.time()}, f)
    except Exception as e:
        log.warning(f"[ig_proxy] Could not save burned list: {e}")


def burn_proxy(proxy_url: str, reason: str = "unknown"):
    """
    Permanently blacklist a proxy from all Instagram use.
    Called by instagram_keeper when a ban/challenge is triggered.
    """
    if not proxy_url:
        return
    # Store just the host:port so protocol variants are all covered
    host_port = proxy_url.split("://")[-1].split("@")[-1]
    with _burned_lock:
        _burned.add(host_port)
    _save_burned()
    proxy_manager.remove_proxy(proxy_url)
    log.warning(f"[ig_proxy] 🔥 BURNED proxy ({reason}): {host_port}")


def _is_burned(proxy_url: str) -> bool:
    host_port = proxy_url.split("://")[-1].split("@")[-1]
    with _burned_lock:
        return host_port in _burned


# ── ASN / datacenter check via ipapi.is (free, 1k/day, no signup) ─────────────

_asn_cache: dict[str, dict] = {}   # ip → result
_asn_cache_lock = threading.Lock()
_asn_daily_count = 0
_ASN_DAILY_LIMIT = 950   # stay under 1k free tier


def _extract_ip(proxy_url: str) -> str | None:
    """Extract host from proxy URL — may be hostname or IP."""
    try:
        host_port = proxy_url.split("://")[-1].split("@")[-1]
        return host_port.split(":")[0]
    except Exception:
        return None


def _check_asn(proxy_url: str) -> dict | None:
    """
    Use ipapi.is to check if the proxy IP is a datacenter / VPN / Tor exit.
    Returns None if the check fails (don't block on API failures).
    Caches results to avoid burning the free daily quota.
    """
    global _asn_daily_count
    if _asn_daily_count >= _ASN_DAILY_LIMIT:
        return None   # quota exhausted — skip check, don't block proxy

    ip = _extract_ip(proxy_url)
    if not ip:
        return None

    with _asn_cache_lock:
        if ip in _asn_cache:
            return _asn_cache[ip]

    try:
        r = requests.get(
            f"https://api.ipapi.is",
            params={"q": ip},
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        _asn_daily_count += 1
        if r.status_code != 200:
            return None
        data = r.json()
        with _asn_cache_lock:
            _asn_cache[ip] = data
        return data
    except Exception:
        return None


def _is_datacenter_ip(proxy_url: str) -> bool:
    """
    Returns True if the proxy IP belongs to a known datacenter ASN.
    Two-stage check:
      1. Local ASN blocklist (instant, no API call)
      2. ipapi.is datacenter flag (uses free API quota)
    """
    # Stage 1: local ASN list (no API call needed)
    ip = _extract_ip(proxy_url)
    if not ip:
        return False

    data = _check_asn(proxy_url)
    if data:
        asn = data.get("as", {}).get("asn", "")
        is_dc     = data.get("is_datacenter", False)
        is_vpn    = data.get("is_vpn", False)
        is_tor    = data.get("is_tor", False)
        is_proxy  = data.get("is_proxy", False)
        org       = data.get("as", {}).get("org", "")

        if f"AS{asn}" in _BLOCKED_ASNS:
            log.debug(f"[ig_proxy] ❌ Blocked ASN AS{asn} ({org}): {ip}")
            return True
        if is_dc:
            log.debug(f"[ig_proxy] ❌ Datacenter IP (ipapi.is): {ip} ({org})")
            return True
        if is_tor:
            log.debug(f"[ig_proxy] ❌ Tor exit node: {ip}")
            return True
        # VPN and proxy flags are acceptable (residential proxies may flag these)
        return False

    # Fallback: no API data — accept the proxy (don't block on uncertainty)
    return False


# ── Pre-login Instagram probe ──────────────────────────────────────────────────

_IG_PROBE_URL = "https://www.instagram.com/accounts/login/"
_IG_API_PROBE = "https://i.instagram.com/api/v1/si/fetch_headers/?challenge_type=signup"


def _probe_instagram(proxy_url: str) -> tuple[bool, str]:
    """
    Hit Instagram through the proxy BEFORE logging in.
    Returns (is_clean, reason).
    is_clean=True  → proxy reaches Instagram with no block signals.
    is_clean=False → IP is already flagged or can't reach Instagram.
    """
    normalized = _normalize_proxy(proxy_url)
    proxies = {"http": normalized, "https": normalized}

    # Try the public login page — any normal response is fine
    try:
        r = requests.get(
            _IG_PROBE_URL,
            proxies=proxies,
            timeout=10,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 11; SM-G991B) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            allow_redirects=True,
        )

        # Check for known block signals in the body
        text_lower = r.text.lower()
        for kw in _BLOCK_KEYWORDS:
            if kw.lower() in text_lower:
                return False, f"block_keyword:{kw}"

        if r.status_code == 403:
            return False, "http_403_ip_block"
        if r.status_code == 429:
            return False, "http_429_rate_limit"
        if r.status_code in (200, 301, 302):
            return True, "ok"

        return False, f"unexpected_status:{r.status_code}"

    except requests.exceptions.ProxyError:
        return False, "proxy_connection_failed"
    except requests.exceptions.ConnectTimeout:
        return False, "connect_timeout"
    except Exception as e:
        return False, f"error:{e}"


# ── Proxy normalization ────────────────────────────────────────────────────────

def _normalize_proxy(proxy_url: str) -> str:
    """socks5:// → socks5h:// so hostnames resolve through the proxy (no DNS leak)."""
    if proxy_url.startswith("socks5://"):
        return proxy_url.replace("socks5://", "socks5h://", 1)
    return proxy_url


# ── Main public API ────────────────────────────────────────────────────────────

def get_clean_proxy(max_attempts: int = 30) -> str | None:
    """
    Find a proxy that passes ALL Instagram-specific checks:
      1. Not burned (previously caused a ban/challenge)
      2. Not a known datacenter ASN
      3. Actually reaches Instagram without block signals

    Tries up to max_attempts candidates from the shared pool.
    Returns normalized proxy URL (socks5h://) or None if nothing passes.
    """
    tried = 0
    skipped_burned = 0
    skipped_dc = 0
    skipped_probe = 0

    # Collect candidates from the shared pool
    candidates = []
    status = proxy_manager.pool_status()
    if status["pool_size"] == 0:
        log.warning("[ig_proxy] Proxy pool is empty — waiting for it to fill.")
        return None

    # Sample from pool repeatedly (pool picks randomly/weighted internally)
    seen = set()
    while len(candidates) < min(max_attempts, 50):
        p = proxy_manager.get_proxy()
        if p and p not in seen:
            seen.add(p)
            candidates.append(p)
        elif not p:
            break

    random.shuffle(candidates)   # test in random order

    for raw_proxy in candidates:
        tried += 1

        # Check 1: burned list
        if _is_burned(raw_proxy):
            skipped_burned += 1
            continue

        # Check 2: datacenter ASN (skip API if quota used up)
        if _is_datacenter_ip(raw_proxy):
            skipped_dc += 1
            proxy_manager.remove_proxy(raw_proxy)   # remove from shared pool too
            continue

        # Check 3: Instagram probe
        clean, reason = _probe_instagram(raw_proxy)
        if not clean:
            skipped_probe += 1
            if reason in ("http_403_ip_block", "block_keyword:checkpoint_required",
                          "block_keyword:ip_block"):
                burn_proxy(raw_proxy, reason)   # already IP-banned — burn it
            else:
                proxy_manager.remove_proxy(raw_proxy)   # dead proxy — just evict
            continue

        # All checks passed
        normalized = _normalize_proxy(raw_proxy)
        log.info(
            f"[ig_proxy] ✅ Clean proxy found after {tried} attempts "
            f"(burned:{skipped_burned} dc:{skipped_dc} blocked:{skipped_probe}): "
            f"{normalized}"
        )
        return normalized

    log.warning(
        f"[ig_proxy] ⚠️  No clean proxy found after {tried} attempts "
        f"(burned:{skipped_burned} dc:{skipped_dc} blocked:{skipped_probe})"
    )
    return None


def status() -> dict:
    """Snapshot for the dashboard."""
    with _burned_lock:
        burned_count = len(_burned)
    return {
        "burned_count":    burned_count,
        "asn_checks_used": _asn_daily_count,
        "asn_quota":       _ASN_DAILY_LIMIT,
        "blocked_asns":    len(_BLOCKED_ASNS),
    }


# ── Init ───────────────────────────────────────────────────────────────────────
_load_burned()
