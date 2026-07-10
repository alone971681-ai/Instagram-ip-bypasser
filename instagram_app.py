"""
instagram_app.py — Instagram Anti-Ban Dashboard
Serves on port 5000 (standalone — run via instagram_main.py).
"""

import os
import time
import instagram_keeper as ig
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "ig-dev-secret")

START_TIME = time.time()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Instagram Keeper</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0f1117; color: #e2e8f0;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 2rem;
    }
    .card {
      background: #1a1d27; border: 1px solid #2d3148; border-radius: 16px;
      padding: 2.5rem; max-width: 560px; width: 100%;
      box-shadow: 0 8px 32px rgba(0,0,0,.4);
    }
    h1 { font-size: 1.6rem; margin-bottom: .25rem; color: #f472b6; }
    .sub { color: #64748b; font-size: .85rem; margin-bottom: 2rem; }
    .section-title {
      font-size: .7rem; text-transform: uppercase; letter-spacing: .08em;
      color: #7c3aed; margin: 1.5rem 0 .5rem;
    }
    .row {
      display: flex; justify-content: space-between; align-items: center;
      padding: .75rem 0; border-bottom: 1px solid #2d3148; font-size: .9rem;
    }
    .row:last-child { border-bottom: none; }
    .label { color: #94a3b8; }
    .badge { padding: .25rem .75rem; border-radius: 999px; font-size: .8rem; font-weight: 600; }
    .ok   { background: #052e16; color: #4ade80; }
    .warn { background: #2d1600; color: #fb923c; }
    .bad  { background: #2d0000; color: #f87171; }
    .pool-list {
      margin-top: 1.25rem; background: #12151e; border: 1px solid #2d3148;
      border-radius: 10px; padding: 1rem; font-size: .78rem;
    }
    .pool-list h3 { color: #7c3aed; margin-bottom: .5rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }
    .pool-list code { display: block; color: #a3e635; padding: .15rem 0; }
    .error-box {
      margin-top: 1.5rem; padding: 1rem; background: #2d0000;
      border: 1px solid #7f1d1d; border-radius: 8px;
      font-size: .85rem; color: #fca5a5; line-height: 1.6;
    }
    .warn-box {
      margin-top: 1.5rem; padding: 1rem; background: #1c0a00;
      border: 1px solid #92400e; border-radius: 8px;
      font-size: .85rem; color: #fbbf24; line-height: 1.6;
    }
    .uptime { margin-top: 1.5rem; text-align: center; color: #475569; font-size: .8rem; }
    #status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                  background: #f472b6; margin-right: 6px; animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
    .account-proxy-box {
      margin-bottom: 1.5rem;
      background: #12151e;
      border: 1px solid #2d3148;
      border-radius: 12px;
      padding: 1rem 1.25rem;
    }
    .account-proxy-box .ap-row {
      display: flex; align-items: center; gap: .75rem; margin-bottom: .5rem;
    }
    .account-proxy-box .ap-row:last-child { margin-bottom: 0; }
    .ap-icon { font-size: 1.1rem; flex-shrink: 0; }
    .ap-label { color: #64748b; font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; }
    .ap-value { color: #e2e8f0; font-size: .92rem; font-weight: 600; word-break: break-all; }
    .ap-value.proxy { color: #a3e635; font-family: monospace; font-size: .82rem; }
    .ap-value.no-proxy { color: #f87171; }
    .arrow { color: #7c3aed; font-size: 1.1rem; margin: .35rem 0 .35rem 1.85rem; }
    .proxy-type-pill {
      display: inline-block; padding: .1rem .5rem; border-radius: 999px;
      font-size: .7rem; font-weight: 700; margin-left: .5rem; vertical-align: middle;
    }
    .pill-socks5 { background: #052e16; color: #4ade80; }
    .pill-http   { background: #1e1b4b; color: #818cf8; }
    .pill-none   { background: #2d0000; color: #f87171; }
  </style>
</head>
<body>
<div class="card">
  <h1>📸 Instagram Keeper</h1>
  <p class="sub"><span id="status-dot"></span>Live status — updates every 10s</p>

  <!-- Account → Proxy summary box -->
  <div class="account-proxy-box">
    <div class="ap-row">
      <span class="ap-icon">👤</span>
      <div>
        <div class="ap-label">Instagram Account</div>
        <div class="ap-value" id="ap-username">—</div>
      </div>
      <span id="ap-login-dot" style="margin-left:auto;font-size:.85rem">…</span>
    </div>
    <div class="arrow">↓ routed through</div>
    <div class="ap-row">
      <span class="ap-icon">🌐</span>
      <div>
        <div class="ap-label">Locked Proxy <span id="ap-type-pill"></span></div>
        <div id="ap-proxy-value" class="ap-value proxy">discovering…</div>
      </div>
    </div>
  </div>

  <p class="section-title">Account Details</p>
  <div class="row">
    <span class="label">Login status</span>
    <span id="login-badge" class="badge">…</span>
  </div>
  <div class="row">
    <span class="label">Username</span>
    <span id="username-badge" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Session age</span>
    <span id="session-age" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Last action</span>
    <span id="last-action" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Total actions</span>
    <span id="action-count" class="badge ok">…</span>
  </div>

  <p class="section-title">Anti-Ban</p>
  <div class="row">
    <span class="label">Heartbeat loop</span>
    <span id="heartbeat-badge" class="badge">…</span>
  </div>
  <div class="row">
    <span class="label">Warmup mode</span>
    <span id="warmup-badge" class="badge">…</span>
  </div>
  <div class="row">
    <span class="label">Cooldown</span>
    <span id="cooldown-badge" class="badge ok">✅ None</span>
  </div>
  <div class="row">
    <span class="label">Actions this hour / today</span>
    <span id="action-rate" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Proxy identity</span>
    <span class="badge ok">✅ Locked per session</span>
  </div>
  <div class="row">
    <span class="label">Device fingerprint</span>
    <span class="badge ok">✅ Random Android (stable)</span>
  </div>
  <div class="row">
    <span class="label">Human delays</span>
    <span class="badge ok">✅ 2–6s per request</span>
  </div>
  <div class="row">
    <span class="label">Session + UUID persistence</span>
    <span class="badge ok">✅ Saved to disk</span>
  </div>
  <div class="row">
    <span class="label">socks5h:// DNS leak fix</span>
    <span class="badge ok">✅ Hostname via proxy</span>
  </div>

  <p class="section-title">IP Intelligence</p>
  <div class="row">
    <span class="label">Datacenter ASNs blocked</span>
    <span id="blocked-asns" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Burned proxies (banned IPs)</span>
    <span id="burned-count" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">ASN reputation checks used</span>
    <span id="asn-checks" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Pre-login Instagram probe</span>
    <span class="badge ok">✅ Runs before every login</span>
  </div>

  <p class="section-title">Proxy Pool</p>
  <div class="row">
    <span class="label">Active proxy</span>
    <span id="proxy-badge" class="badge">…</span>
  </div>
  <div class="row">
    <span class="label">Pool size</span>
    <span id="pool-size" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">SOCKS5 / HTTP</span>
    <span id="pool-types" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Avg latency</span>
    <span id="pool-latency" class="badge ok">…</span>
  </div>
  <div class="row">
    <span class="label">Pool refreshing</span>
    <span id="pool-refresh" class="badge">…</span>
  </div>

  <div id="pool-box" class="pool-list" style="display:none">
    <h3>Working proxies in pool</h3>
    <div id="pool-entries"></div>
  </div>

  <div id="error-box" class="error-box" style="display:none">
    ❌ <strong>Login error:</strong> <span id="error-msg"></span><br>
    Check <strong>IG_USERNAME</strong> and <strong>IG_PASSWORD</strong> in Replit Secrets.
  </div>

  <div id="warn-box" class="warn-box" style="display:none">
    ⚠️ <strong>No proxy set yet</strong><br>
    Instagram on Replit's datacenter IP gets flagged quickly.
    The proxy pool is warming up automatically — sit tight.
  </div>

  <p class="uptime" id="uptime-line">Uptime: …</p>
</div>

<script>
function fmt(s) {
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h ? h+'h '+m+'m '+sec+'s' : m ? m+'m '+sec+'s' : sec+'s';
}
function ago(ts) {
  if (!ts) return '—';
  const s = Math.floor(Date.now()/1000 - ts);
  return s < 5 ? 'just now' : fmt(s) + ' ago';
}
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    // ── Account → Proxy summary box ──────────────────────────────
    document.getElementById('ap-username').textContent = d.username || '—';
    document.getElementById('ap-login-dot').textContent = d.logged_in ? '🟢 Online' : '🔴 Offline';

    const apProxy = document.getElementById('ap-proxy-value');
    const apPill  = document.getElementById('ap-type-pill');
    if (d.active_proxy) {
      // strip credentials, show only host:port
      const clean = d.active_proxy.replace(/^[a-z0-9+]+:[/][/]/, '').replace(/.*@/, '');
      const isSocks = d.active_proxy.startsWith('socks5');
      apProxy.textContent = clean;
      apProxy.className = 'ap-value proxy';
      apPill.innerHTML = isSocks
        ? '<span class="proxy-type-pill pill-socks5">SOCKS5</span>'
        : '<span class="proxy-type-pill pill-http">HTTP</span>';
    } else if (d.refreshing) {
      apProxy.textContent = 'Discovering proxies…';
      apProxy.className = 'ap-value';
      apPill.innerHTML = '<span class="proxy-type-pill pill-none">none yet</span>';
    } else {
      apProxy.textContent = '⚠️ No proxy — direct Replit IP!';
      apProxy.className = 'ap-value no-proxy';
      apPill.innerHTML = '<span class="proxy-type-pill pill-none">NONE</span>';
    }
    // ─────────────────────────────────────────────────────────────

    // login
    const lb = document.getElementById('login-badge');
    lb.textContent = d.logged_in ? '✅ Logged in' : '❌ Not logged in';
    lb.className = 'badge ' + (d.logged_in ? 'ok' : 'bad');

    document.getElementById('username-badge').textContent = d.username || '—';

    // session age
    document.getElementById('session-age').textContent =
      d.session_start ? fmt(Date.now()/1000 - d.session_start) : '—';

    // last action
    const la = document.getElementById('last-action');
    la.textContent = d.last_action ? d.last_action.replace(/_/g,' ') + '  (' + ago(d.last_action_ts) + ')' : '—';

    document.getElementById('action-count').textContent = d.action_count + ' actions';

    // heartbeat
    const hb = document.getElementById('heartbeat-badge');
    hb.textContent = d.heartbeat_active ? '✅ Active (every 10–25 min)' : '⏸ Idle';
    hb.className = 'badge ' + (d.heartbeat_active ? 'ok' : 'warn');

    // warmup
    const wb = document.getElementById('warmup-badge');
    wb.textContent = d.warmup_active ? '🔥 Running (read-only ramp)' : 'Not running';
    wb.className = 'badge ' + (d.warmup_active ? 'warn' : 'ok');

    // cooldown
    const cb = document.getElementById('cooldown-badge');
    const now = Math.floor(Date.now()/1000);
    if (d.cooldown_until && d.cooldown_until > now) {
      const rem = Math.ceil((d.cooldown_until - now) / 60);
      cb.textContent = '💤 ' + rem + ' min remaining (' + (d.error_type||'') + ')';
      cb.className = 'badge warn';
    } else {
      cb.textContent = '✅ None';
      cb.className = 'badge ok';
    }

    // action rate
    document.getElementById('action-rate').textContent =
      (d.actions_last_hour||0) + ' / hr  ·  ' + (d.actions_today||0) + ' / day';

    // IP intelligence
    document.getElementById('blocked-asns').textContent =
      (d.blocked_asns||0) + ' ASNs (AWS, DO, Hetzner…)';
    const bc = document.getElementById('burned-count');
    bc.textContent = (d.burned_count||0) + ' permanently blacklisted';
    bc.className = 'badge ' + ((d.burned_count||0) === 0 ? 'ok' : 'warn');
    document.getElementById('asn-checks').textContent =
      (d.asn_checks_used||0) + ' / ' + (d.asn_quota||950) + ' today (free tier)';

    // proxy
    const pb = document.getElementById('proxy-badge');
    if (d.active_proxy) {
      pb.textContent = '✅ ' + d.active_proxy.split('@').pop();
      pb.className = 'badge ok';
    } else if (d.pool_size > 0) {
      pb.textContent = '🔄 Pool ready, assigning…';
      pb.className = 'badge warn';
    } else {
      pb.textContent = d.refreshing ? '🔄 Discovering…' : '❌ None yet';
      pb.className = 'badge ' + (d.refreshing ? 'warn' : 'bad');
    }

    // pool
    const ps = document.getElementById('pool-size');
    ps.textContent = d.pool_size + ' working';
    ps.className = 'badge ' + (d.pool_size >= 3 ? 'ok' : d.pool_size > 0 ? 'warn' : 'bad');

    document.getElementById('pool-types').textContent =
      d.socks5_count + ' SOCKS5  /  ' + d.http_count + ' HTTP';

    const pl = document.getElementById('pool-latency');
    pl.textContent = d.pool_size ? d.avg_latency + ' ms avg' : '—';
    pl.className = 'badge ' + (d.avg_latency < 500 ? 'ok' : d.avg_latency < 1500 ? 'warn' : 'bad');

    const pr = document.getElementById('pool-refresh');
    pr.textContent = d.refreshing ? '🔄 Yes' : 'No (auto every 1h)';
    pr.className = 'badge ' + (d.refreshing ? 'warn' : 'ok');

    // pool list
    const box = document.getElementById('pool-box');
    const entries = document.getElementById('pool-entries');
    if (d.pool_sample && d.pool_sample.length) {
      box.style.display = 'block';
      entries.innerHTML = d.pool_sample.map(p => '<code>' + p + '</code>').join('');
      if (d.pool_size > d.pool_sample.length)
        entries.innerHTML += '<code style="color:#64748b">… and ' + (d.pool_size - d.pool_sample.length) + ' more</code>';
    } else { box.style.display = 'none'; }

    // error box
    const eb = document.getElementById('error-box');
    if (d.error) {
      eb.style.display = 'block';
      document.getElementById('error-msg').textContent = d.error;
    } else { eb.style.display = 'none'; }

    // warn box (no proxy)
    document.getElementById('warn-box').style.display =
      (!d.active_proxy && d.pool_size === 0 && !d.refreshing) ? 'block' : 'none';

    document.getElementById('uptime-line').textContent =
      'Uptime: ' + fmt(d.uptime);
  } catch(e) { /* server restarting */ }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/api/status")
def api_status():
    s = ig.status()
    s["uptime"] = time.time() - START_TIME
    return jsonify(s)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "uptime": time.time() - START_TIME})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
