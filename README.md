# Instagram Anti-Ban Keeper

Keeps your Instagram account logged in and active 24/7 through clean, filtered proxies — built for accounts that have already been IP-banned once and can't afford another strike.

---

## 🚀 How to Use It

### 1. Add your credentials (Replit Secrets, not the dashboard)
Open the **Secrets** tab (🔒 icon in the sidebar) and add:

| Key | Value |
|---|---|
| `IG_USERNAME` | Your Instagram username |
| `IG_PASSWORD` | Your Instagram password |

Optional:

| Key | Value |
|---|---|
| `PROXY_URL` | A specific residential proxy to force (overrides auto-discovery) |

### 2. Start it
Run the **Start Instagram** workflow. On startup it will:
1. Build a pool of 1000+ candidate proxies from 12 sources
2. Test them and keep the fastest ~15
3. Filter Instagram-only through the 4-layer IP intelligence check
4. Log in using your saved session (or fresh login if none exists)
5. Begin the heartbeat loop

### 3. Watch the dashboard
Open the web preview on port 5000. It shows:
- Login status and which proxy is locked to your account
- Action count and last activity time
- IP intelligence stats — blocked ASNs, burned proxies, reputation checks used

### 4. If something's wrong
The dashboard/log will tell you exactly what happened:
- `IG_USERNAME or IG_PASSWORD is not set` → add them to Secrets
- `Wrong password` → check your credentials
- `2FA enabled` → temporarily disable 2FA on the account
- `Challenge required` → the proxy that triggered it has already been burned and blacklisted automatically

---

## 🛡️ Features

### Proxy Intelligence (4 layers, before login ever happens)
1. **Datacenter ASN filter** — blocks 33+ known ranges (AWS, DigitalOcean, Hetzner, GCP, Azure, OVH, Vultr, etc.)
2. **Reputation check** — live lookup against `ipapi.is` to catch VPN/Tor/datacenter IPs not in the static list
3. **Burned proxy blacklist** — any proxy that ever triggered a ban/challenge is saved to `ig_burned.json` and never reused, even after restart
4. **Live pre-login probe** — tests the proxy against Instagram directly before your account ever touches it

### Session & Device Persistence
- Full device fingerprint (phone_id, uuid, adid) generated once and reused every restart
- Instagram sees "the same phone coming back online," never a new unknown device
- Session saved to `ig_session.json` — skips full re-login when possible

### Stable Proxy Locking
- One proxy is locked to your account for the entire session
- Never rotates unless the proxy dies or gets burned — constant IP switching is itself a red flag to Instagram

### Human Behavior Simulation
- Heartbeat loop runs every 10–25 minutes: browses feed, checks inbox, views story tray, checks notifications
- Randomized delays between actions to avoid robotic timing patterns
- Warmup phase for new sessions — read-only actions first, write actions introduced slowly

### Smart Rate Limiting
Community-verified safe action ceilings, enforced automatically:

| Action | Per Hour | Per Day |
|---|---|---|
| Likes | 20 | 300 |
| Follows | 10 | 100 |
| Comments | 5 | 30 |
| DMs | 5 | 20 |
| Story views | 30 | 100 |
| Feed/inbox checks | 60 | 300 |

### Tiered Error Handling
Each Instagram error is treated differently instead of blind retries:

| Error | Response |
|---|---|
| Rate limited (429) | Pause 10–15 min, same proxy |
| "Please wait a few minutes" | Pause 30 min, same proxy (more serious than 429) |
| Action blocked (feedback required) | Pause 60 min, freeze that action type |
| Challenge required | Burn the proxy permanently, pause 2 hours, get a new clean proxy |
| Connection failure | Evict the proxy (not burned), find a replacement |

### DNS Leak Protection
Uses `socks5h://` so hostname resolution happens through the proxy itself — never leaks your real network through local DNS.

---

## 📁 Key Files

| File | Purpose |
|---|---|
| `instagram_main.py` | Entry point — starts the dashboard + keeper |
| `instagram_keeper.py` | Core anti-ban engine — login, heartbeat, limits, error handling |
| `instagram_proxy.py` | IP intelligence layer — the 4-layer proxy filter |
| `instagram_app.py` | Flask dashboard |
| `proxy_manager.py` | Shared proxy pool (also used by the Discord keeper) |

## ⚠️ Note
This tool is meant to help legitimate account owners avoid a repeat ban after already being banned once — not for spam or abuse automation. Respect the built-in rate limits.
