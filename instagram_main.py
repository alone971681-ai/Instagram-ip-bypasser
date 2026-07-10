"""
Instagram Account Keeper — Anti-Ban Edition
============================================
Standalone entry point for Instagram. Run this instead of main.py.

Quick start:
  1. Add IG_USERNAME in Replit → Secrets (🔒)  — your Instagram username
  2. Add IG_PASSWORD in Secrets                 — your Instagram password
  3. Hit Run — proxy discovery starts automatically.

Optional: add PROXY_URL in Secrets to force a specific residential proxy
          (overrides auto-discovery, recommended for serious use).

How it works:
  • All Instagram traffic is routed through residential/free proxies
    so Instagram never sees Replit's datacenter IP.
  • A heartbeat loop browses your feed / inbox every 8–20 min like a
    real human, preventing idle-account flags.
  • The proxy pool is shared with the Discord keeper (proxy_manager.py)
    and refreshes automatically every hour.
"""

import os
import time
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ig_main")

# ── Start proxy discovery immediately (non-blocking) ──────────────────────────
import proxy_manager
proxy_manager.start()

# ── Flask dashboard (background thread) ───────────────────────────────────────
def _start_flask():
    try:
        from instagram_app import app as flask_app
        flask_app.run(host="0.0.0.0", port=5000, use_reloader=False, debug=False)
    except Exception as e:
        log.warning(f"Flask dashboard unavailable: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    # Start dashboard first — keeps preview alive regardless of login status
    flask_thread = threading.Thread(target=_start_flask, daemon=True)
    flask_thread.start()
    log.info("📊  Dashboard running at port 5000")

    # Validate secrets
    ig_user = os.environ.get("IG_USERNAME", "")
    ig_pass = os.environ.get("IG_PASSWORD", "")

    if not ig_user or not ig_pass:
        log.error("IG_USERNAME or IG_PASSWORD is not set.")
        log.error("→ Open Replit Secrets (🔒) and add both values.")
        log.info("Dashboard running — check the preview pane.")
        while True:
            time.sleep(60)

    # Start the Instagram keeper (login + heartbeat loop)
    import instagram_keeper as ig
    ig.start()

    log.info("🚀  Instagram Keeper is running. Press Ctrl-C to stop.")

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Shutting down…")
        ig.stop()


if __name__ == "__main__":
    main()
