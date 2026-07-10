"""
instagram_keeper.py — Instagram Anti-Ban Engine (v2)
=====================================================
Research sources:
  • https://subzeroid.github.io/instagrapi/usage-guide/best-practices.html
  • https://github.com/subzeroid/instagrapi (6.4k stars, actively maintained fork)
  • Community-verified action limits (2025-2026)

Key principles applied from research:
  1. ONE stable proxy per account — never rotate mid-session or mid-challenge.
     Instagram ties trust to consistent IP + device + session history.
  2. Persist full UUID/device settings blob (phone_id, uuid, adid, etc.) so
     Instagram always sees the same "device" across restarts.
  3. Use socks5h:// (not socks5://) so hostnames resolve through the proxy.
  4. Handle each error class differently — throttle ≠ challenge ≠ feedback block.
  5. Warmup = read-only actions first, write actions much later & slowly.
  6. Respect community-verified action limits to avoid trigger points.
"""

import os
import time
import json
import random
import logging
import threading

import proxy_manager
import instagram_proxy as ig_proxy

log = logging.getLogger("ig_keeper")

# ── Config ─────────────────────────────────────────────────────────────────────
IG_USERNAME   = os.environ.get("IG_USERNAME", "")
IG_PASSWORD   = os.environ.get("IG_PASSWORD", "")
SESSION_FILE  = "ig_session.json"   # full settings blob (UUIDs + cookies + device)

# ── Community-verified safe action limits (2025-2026) ─────────────────────────
# Source: https://elfsight.com/blog/instagram-restrictions-limits-likes-followers-comments/
# New accounts are stricter — established accounts can do more over time.
LIMITS = {
    # (per_hour, per_day)
    "like":    (20,  300),   # new account safe zone
    "follow":  (10,  100),
    "comment": (5,   30),
    "dm":      (5,   20),
    "story":   (30,  100),   # story views are read-only, much safer
    "feed":    (60,  300),   # browsing feed/inbox — very safe
}

# ── Shared state (read by instagram_app.py dashboard) ─────────────────────────
state = {
    "logged_in":        False,
    "username":         IG_USERNAME or "—",
    "active_proxy":     None,
    "last_action":      None,
    "last_action_ts":   0.0,
    "action_count":     0,
    "session_age":      0.0,
    "session_start":    0.0,
    "error":            None,
    "error_type":       None,   # "throttle" | "challenge" | "feedback" | "banned"
    "cooldown_until":   0.0,
    "heartbeat_active": False,
    "warmup_active":    False,
    "proxy_locked":     False,  # True once a working proxy is assigned and locked
}
_state_lock = threading.Lock()
_action_counts: dict[str, list[float]] = {}   # action_type -> [timestamps]
_action_counts_lock = threading.Lock()


def _set(key, value):
    with _state_lock:
        state[key] = value


def _get(key):
    with _state_lock:
        return state[key]


# ── Action rate tracking ───────────────────────────────────────────────────────

def _check_limit(action: str) -> bool:
    """Return True if we're within safe limits for this action type."""
    if action not in LIMITS:
        return True
    per_hour, per_day = LIMITS[action]
    now = time.time()
    with _action_counts_lock:
        timestamps = _action_counts.get(action, [])
        # prune old timestamps
        timestamps = [t for t in timestamps if now - t < 86400]
        last_hour = [t for t in timestamps if now - t < 3600]
        _action_counts[action] = timestamps
        if len(last_hour) >= per_hour:
            log.warning(f"[ig] ⚠️  Hourly limit reached for '{action}' ({per_hour}/hr) — skipping.")
            return False
        if len(timestamps) >= per_day:
            log.warning(f"[ig] ⚠️  Daily limit reached for '{action}' ({per_day}/day) — skipping.")
            return False
    return True


def _record_action_count(action: str):
    now = time.time()
    with _action_counts_lock:
        if action not in _action_counts:
            _action_counts[action] = []
        _action_counts[action].append(now)
    with _state_lock:
        state["last_action"]    = action
        state["last_action_ts"] = now
        state["action_count"]  += 1
        state["session_age"]    = now - state["session_start"] if state["session_start"] else 0


# ── Device fingerprints ────────────────────────────────────────────────────────
# Based on real Android device specs. Pick one at client creation and KEEP IT.
_DEVICES = [
    {"manufacturer": "Samsung",  "model": "SM-G991B",  "android_version": 30, "android_release": "11.0"},
    {"manufacturer": "Google",   "model": "Pixel 6",   "android_version": 31, "android_release": "12.0"},
    {"manufacturer": "OnePlus",  "model": "IN2023",    "android_version": 30, "android_release": "11.0"},
    {"manufacturer": "Xiaomi",   "model": "M2012K11AG","android_version": 30, "android_release": "11.0"},
    {"manufacturer": "Samsung",  "model": "SM-A525F",  "android_version": 31, "android_release": "12.0"},
]

# ── Lazy import (instagrapi may not be installed in Discord-only mode) ─────────
def _import_instagrapi():
    try:
        from instagrapi import Client
        from instagrapi.exceptions import (
            LoginRequired, ChallengeRequired, BadPassword,
            TwoFactorRequired, ReloginAttemptExceeded,
            ClientThrottledError, FeedbackRequired,
            PleaseWaitFewMinutes, ClientError,
        )
        return Client, {
            "LoginRequired": LoginRequired,
            "ChallengeRequired": ChallengeRequired,
            "BadPassword": BadPassword,
            "TwoFactorRequired": TwoFactorRequired,
            "ReloginAttemptExceeded": ReloginAttemptExceeded,
            "ClientThrottledError": ClientThrottledError,
            "FeedbackRequired": FeedbackRequired,
            "PleaseWaitFewMinutes": PleaseWaitFewMinutes,
            "ClientError": ClientError,
        }
    except ImportError:
        return None, {}


# ── Proxy helpers ──────────────────────────────────────────────────────────────

def _normalize_proxy(raw: str | None) -> str | None:
    """
    Convert socks5:// → socks5h:// so hostnames resolve through the proxy.
    This is required by instagrapi best practices to avoid DNS leaks.
    """
    if not raw:
        return None
    if raw.startswith("socks5://"):
        return raw.replace("socks5://", "socks5h://", 1)
    return raw


def _assign_stable_proxy() -> str | None:
    """
    Pick ONE clean proxy and lock it to this account session.
    Uses instagram_proxy.get_clean_proxy() which filters:
      - Burned IPs (previously caused bans)
      - Known datacenter ASNs (AWS/DO/Hetzner etc.)
      - IPs already blocked by Instagram (pre-login probe)
    """
    if _get("proxy_locked") and _get("active_proxy"):
        return _get("active_proxy")   # reuse the locked proxy

    # Manual override skips all checks — user trusts their own proxy
    manual = os.environ.get("PROXY_URL", "").strip()
    if manual:
        proxy = _normalize_proxy(manual)
        _set("active_proxy", proxy)
        _set("proxy_locked", True)
        log.info(f"[ig] 🔒 Manual proxy locked: {proxy}")
        return proxy

    log.info("[ig] 🔍 Finding a clean Instagram proxy (datacenter filter + probe)…")
    proxy = ig_proxy.get_clean_proxy()

    if proxy:
        _set("active_proxy", proxy)
        _set("proxy_locked", True)
        log.info(f"[ig] 🔒 Clean proxy locked: {proxy}")
    else:
        _set("active_proxy", None)
        log.warning("[ig] ⚠️  No clean proxy found — connecting direct (high ban risk!)")
    return proxy


def _unlock_and_swap_proxy(burn: bool = False, reason: str = "dead"):
    """
    Called only when the locked proxy has provably failed.
    burn=True  → permanently blacklists the proxy (ban/challenge triggered).
    burn=False → just evicts it (connection failure).
    """
    old = _get("active_proxy")
    if old:
        if burn:
            ig_proxy.burn_proxy(old, reason)
        else:
            proxy_manager.remove_proxy(old)
        log.warning(f"[ig] 🔄 Releasing proxy ({'burned' if burn else 'evicted'}): {old}")
    _set("proxy_locked", False)
    _set("active_proxy", None)
    return _assign_stable_proxy()


# ── Client lifecycle ───────────────────────────────────────────────────────────
_client      = None
_client_lock = threading.Lock()
_exc         = {}   # populated on first import


def _make_client():
    Client, exc = _import_instagrapi()
    if Client is None:
        log.error("[ig] instagrapi not installed — run: pip install instagrapi")
        return None
    global _exc
    _exc = exc

    cl = Client()
    cl.delay_range = [2, 6]   # instagrapi built-in: random delay after every request

    proxy = _assign_stable_proxy()
    if proxy:
        cl.set_proxy(proxy)

    device = random.choice(_DEVICES)
    cl.set_device(device)
    # Set locale/timezone to match a real user (neutral US)
    cl.set_locale("en_US")
    cl.set_timezone_offset(-18000)   # UTC-5 (Eastern)
    return cl


def _save_session(cl):
    """
    Save the FULL settings blob including UUIDs (phone_id, uuid, adid).
    Research: Instagram needs to see the same device on every reconnect.
    """
    try:
        cl.dump_settings(SESSION_FILE)
        log.info(f"[ig] 💾 Full session (UUIDs + cookies) saved → {SESSION_FILE}")
    except Exception as e:
        log.warning(f"[ig] Could not save session: {e}")


def _load_session(cl) -> bool:
    """
    Restore saved session so we reuse the same device UUIDs.
    This is the #1 anti-ban practice — avoid fresh device fingerprints.
    """
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        cl.load_settings(SESSION_FILE)
        cl.login(IG_USERNAME, IG_PASSWORD)
        log.info("[ig] ✅ Session + UUIDs restored from disk.")
        return True
    except Exception:
        log.info("[ig] Saved session expired — doing fresh login.")
        return False


def _do_login(cl) -> bool:
    if _load_session(cl):
        return True

    log.info("[ig] 🔑 Fresh login…")
    try:
        cl.login(IG_USERNAME, IG_PASSWORD)
        return True
    except Exception as e:
        name = type(e).__name__
        if "BadPassword" in name:
            log.error("[ig] ❌ Wrong password — check IG_PASSWORD secret.")
            _set("error", "Bad password"); _set("error_type", "banned")
        elif "TwoFactorRequired" in name:
            log.error("[ig] ❌ 2FA enabled — disable it or handle the code.")
            _set("error", "2FA required"); _set("error_type", "challenge")
        elif "ChallengeRequired" in name:
            log.warning("[ig] ⚠️  Challenge at login — Instagram wants verification.")
            _set("error", "Challenge required"); _set("error_type", "challenge")
        else:
            log.error(f"[ig] ❌ Login failed: {e}")
            _set("error", str(e)); _set("error_type", "unknown")
        return False


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            cl = _make_client()
            if cl is None:
                return None
            if _do_login(cl):
                _save_session(cl)
                now = time.time()
                _set("logged_in", True)
                _set("username", cl.username)
                _set("session_start", now)
                _set("error", None)
                _set("error_type", None)
                _client = cl
                log.info(f"[ig] ✅ Ready as @{cl.username}")
            else:
                _client = None
        return _client


def _reset_client():
    global _client
    with _client_lock:
        _client = None
        _set("logged_in", False)
        _set("proxy_locked", False)


# ── Cooldown helpers ───────────────────────────────────────────────────────────

def _in_cooldown() -> bool:
    until = _get("cooldown_until")
    if until and time.time() < until:
        remaining = until - time.time()
        log.info(f"[ig] 💤 In cooldown — {remaining/60:.1f} min remaining.")
        return True
    return False


def _set_cooldown(minutes: float, reason: str):
    until = time.time() + minutes * 60
    _set("cooldown_until", until)
    log.warning(f"[ig] 💤 Cooldown set: {minutes:.0f} min ({reason})")


# ── Safe action wrapper ────────────────────────────────────────────────────────

def _safe_action(fn, *args, action_name="action", **kwargs):
    """
    Execute fn(*args) with full error classification per instagrapi best practices.
    Each error type gets the correct response instead of a blanket retry.
    """
    if _in_cooldown():
        return None

    if not _check_limit(action_name):
        return None

    cl = get_client()
    if cl is None:
        return None

    exc = _exc
    try:
        result = fn(*args, **kwargs)
        _record_action_count(action_name)
        return result

    except exc.get("ClientThrottledError", Exception):
        # HTTP 429 — current IP/pattern too aggressive right now
        log.warning("[ig] ⚡ Throttled (429) — backing off 15 min.")
        _set("error", "Throttled"); _set("error_type", "throttle")
        _set_cooldown(15, "ClientThrottledError / 429")
        return None

    except exc.get("PleaseWaitFewMinutes", Exception):
        # More serious than 429 — account/device/IP being warned
        log.warning("[ig] ⏳ PleaseWaitFewMinutes — pausing write actions 30 min.")
        _set("error", "Please wait"); _set("error_type", "throttle")
        _set_cooldown(30, "PleaseWaitFewMinutes")
        return None

    except exc.get("FeedbackRequired", Exception):
        # Action blocked — burn the proxy if it's an IP-level block
        msg = getattr(cl, "last_json", {})
        feedback = msg.get("feedback_message", "unknown") if isinstance(msg, dict) else "unknown"
        log.warning(f"[ig] 🚫 FeedbackRequired — {feedback}. Freezing '{action_name}' 60 min.")
        _set("error", f"Feedback: {feedback}"); _set("error_type", "feedback")
        _set_cooldown(60, f"FeedbackRequired on {action_name}")
        # If the feedback indicates an IP-level block, burn the proxy
        if any(kw in str(feedback).lower() for kw in ("ip", "block", "spam", "abuse")):
            _unlock_and_swap_proxy(burn=True, reason=f"FeedbackRequired:{feedback}")
        return None

    except exc.get("ChallengeRequired", Exception):
        # Instagram wants verification — BURN the proxy (it caused the challenge)
        # and do NOT rotate to another dirty proxy — find a clean one
        log.warning("[ig] 🔐 ChallengeRequired — burning proxy, finding clean one, pausing 2h.")
        _set("error", "Challenge required"); _set("error_type", "challenge")
        _set_cooldown(120, "ChallengeRequired — manual review needed")
        _unlock_and_swap_proxy(burn=True, reason="ChallengeRequired")
        return None

    except exc.get("LoginRequired", Exception):
        # Session expired — re-login with same device settings, same proxy
        log.warning("[ig] 🔑 Session expired — re-logging in (same device UUIDs).")
        _reset_client()
        cl2 = get_client()
        if cl2 is None:
            return None
        try:
            result = fn(*args, **kwargs)
            _record_action_count(action_name)
            return result
        except Exception as e2:
            log.error(f"[ig] Action failed after re-login: {e2}")
            return None

    except exc.get("ReloginAttemptExceeded", Exception):
        log.error("[ig] ❌ Re-login attempts exceeded — burning proxy, freezing 10 min.")
        _set_cooldown(10, "ReloginAttemptExceeded")
        _unlock_and_swap_proxy(burn=True, reason="ReloginAttemptExceeded")
        return None

    except exc.get("ClientError", Exception) as e:
        # Proxy transport failure — evict (not burn) and swap
        proxy = _get("active_proxy")
        if proxy:
            log.warning(f"[ig] 🌐 ClientError (proxy dead?) — evicting: {e}")
            _unlock_and_swap_proxy(burn=False, reason="ClientError")
            _reset_client()
        else:
            log.warning(f"[ig] ClientError (no proxy): {e}")
        return None

    except Exception as e:
        log.warning(f"[ig] Unexpected error in '{action_name}': {e}")
        return None


# ── Read-only heartbeat actions (safe, mimic idle user) ───────────────────────

def _browse_feed():
    cl = get_client()
    if cl:
        _safe_action(cl.get_timeline_feed, action_name="feed")


def _check_inbox():
    cl = get_client()
    if cl:
        _safe_action(cl.direct_threads, action_name="feed")


def _check_notifications():
    cl = get_client()
    if cl:
        _safe_action(cl.news_inbox_v1, action_name="feed")


def _view_stories():
    """View story trays — very low-risk read action."""
    cl = get_client()
    if cl:
        _safe_action(cl.get_reels_tray_feed, action_name="story")


# ── Human-like pause ───────────────────────────────────────────────────────────

def _pause(min_s=5, max_s=20):
    """Simulate reading/thinking time between actions."""
    time.sleep(random.uniform(min_s, max_s))


# ── Heartbeat loop ─────────────────────────────────────────────────────────────
_stop_event = threading.Event()


def _heartbeat_loop():
    _set("heartbeat_active", True)
    log.info("[ig] 💓 Heartbeat loop started — read-only actions every 10–25 min")

    # Read-only actions only in heartbeat — safest pattern per research
    actions = [
        (_browse_feed,          "browse_feed"),
        (_check_inbox,          "check_inbox"),
        (_check_notifications,  "check_notifications"),
        (_view_stories,         "view_stories"),
    ]

    while not _stop_event.is_set():
        if not _in_cooldown():
            action_fn, action_name = random.choice(actions)
            log.info(f"[ig] 💓 Heartbeat: {action_name}")
            action_fn()
            _pause(5, 15)

        # Wait 10–25 minutes between heartbeat cycles
        wait = random.uniform(600, 1500)
        log.info(f"[ig] Next heartbeat in {wait/60:.1f} min")
        _stop_event.wait(wait)

    _set("heartbeat_active", False)
    log.info("[ig] Heartbeat stopped.")


# ── Warmup (for fresh / recently challenged accounts) ─────────────────────────

def run_warmup(hours: float = 1.0):
    """
    Research-based warmup strategy:
    Phase 1 (first 50% of time): read-only feed browsing + story views only.
    Phase 2 (second 50%):        add inbox checks + notification checks.
    Never write (follow/like/comment) during warmup — ramp that separately over days.
    """
    _set("warmup_active", True)
    log.info(f"[ig] 🔥 Warmup started ({hours}h) — read-heavy phase first")
    total_secs = hours * 3600
    end_time   = time.time() + total_secs
    phase1_end = time.time() + total_secs * 0.5
    actions_done = 0

    # Phase 1 — absolute minimum: feed only
    while time.time() < phase1_end and not _stop_event.is_set():
        _browse_feed()
        actions_done += 1
        gap = random.uniform(900, 1800)   # 15–30 min gaps (very slow)
        if time.time() + gap > end_time:
            break
        _stop_event.wait(gap)

    # Phase 2 — add inbox + notifications
    log.info("[ig] 🔥 Warmup phase 2: adding inbox + notification checks")
    while time.time() < end_time and not _stop_event.is_set():
        action = random.choice([_browse_feed, _check_inbox, _check_notifications, _view_stories])
        action()
        actions_done += 1
        gap = random.uniform(600, 1200)   # 10–20 min gaps
        if time.time() + gap > end_time:
            break
        _stop_event.wait(gap)

    _set("warmup_active", False)
    log.info(f"[ig] ✅ Warmup complete — {actions_done} read-only actions over {hours}h")


# ── Public API ─────────────────────────────────────────────────────────────────

def start():
    """
    Call once at startup. Waits for proxy pool, then logs in and starts heartbeat.
    """
    if not IG_USERNAME or not IG_PASSWORD:
        log.error("[ig] IG_USERNAME / IG_PASSWORD not set — Instagram keeper idle.")
        return

    log.info("[ig] ⏳ Waiting for proxy pool (up to 90s)…")
    deadline = time.time() + 90
    while time.time() < deadline:
        if proxy_manager.get_proxy():
            break
        time.sleep(2)

    cl = get_client()
    if cl is None:
        log.error("[ig] Could not log in — heartbeat not started.")
        return

    t = threading.Thread(target=_heartbeat_loop, daemon=True)
    t.start()


def stop():
    _stop_event.set()


def status() -> dict:
    """Snapshot for the dashboard."""
    with _state_lock:
        snap = dict(state)
    ps = proxy_manager.pool_status()
    snap["pool_size"]    = ps["pool_size"]
    snap["socks5_count"] = ps["socks5_count"]
    snap["http_count"]   = ps["http_count"]
    # IP intelligence stats from instagram_proxy layer
    ip_stats = ig_proxy.status()
    snap["burned_count"]    = ip_stats["burned_count"]
    snap["asn_checks_used"] = ip_stats["asn_checks_used"]
    snap["asn_quota"]       = ip_stats["asn_quota"]
    snap["blocked_asns"]    = ip_stats["blocked_asns"]
    snap["avg_latency"]  = ps["avg_latency"]
    snap["refreshing"]   = ps["refreshing"]
    snap["pool_sample"]  = ps["sample"]

    # Action counts summary for dashboard
    now = time.time()
    with _action_counts_lock:
        snap["actions_last_hour"] = sum(
            1 for ts_list in _action_counts.values()
            for t in ts_list if now - t < 3600
        )
        snap["actions_today"] = sum(
            1 for ts_list in _action_counts.values()
            for t in ts_list if now - t < 86400
        )
    return snap
