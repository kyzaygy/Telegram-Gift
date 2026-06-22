# Surf Sniper

Monitors the live mint counter for **Surge Board** collectible gifts via HTTP
probing and upgrades two surfs to target numbers **444** and **666** at the
precisely right moment.

> **Known unverifiable risks:**
> - The paid upgrade path (`SendStarsForm`) cannot be tested before firing —
>   the signature is confirmed by introspection (M5) but actual payment only
>   on the real shot.
> - HTTP probing is not instantaneous. At high mint rates a rapid burst could
>   cause a miss; the surf is **not consumed** in that case (ABORT rule).
> - `availability_issued` in the Telegram NFT page HTML is frozen at mint time —
>   it is not a live counter and is not used here.
> - The standard slug for issue #444 might not be `SurgeBoard-444`; the bot
>   triggers on the **maximum existing issue number**, not a specific slug.
> - You need ~14 780 stars on balance at fire time.
> - `TgCrypto` is optional; Pyrogram works without it (slightly slower crypto),
>   which is fine for this workload.

---

## Architecture

```
src/
  config.py          .env + targets.yaml loader (validates unknown fields, logs applied values)
  login.py           One-time QR + 2FA login → probe.session
  state.py           JSON persistence (fired surfs survive restarts)
  tg.py              Kurigram Client; fails fast if session missing
  issued_probe.py    HTTP binary search → current_issue()
  watcher.py         Adaptive polling loop + form prefetch in tight zone + fire trigger
  fire.py            Final check, GetPaymentForm, SendStarsForm (invoice fallback), result parse
  shared.py          In-memory state shared with web dashboard
  web.py             FastAPI dashboard (same asyncio loop)
  main.py            Orchestrator
systemd/surfsniper.service
```

### How issue detection works

`current_issue()` fetches `https://t.me/nft/SurgeBoard-N` and checks `og:title`:
- **Minted** → title does not start with `"Telegram"` (shows the gift name)
- **Not minted** → Telegram redirects to `telegram.org`, title = `"Telegram – a new era of messaging"`
- **Network error** → retried up to 3×, never treated as "not minted"

Finding the maximum N: exponential expansion → binary search → linear scan
with `hole_tolerance` consecutive misses to tolerate individually-missing slugs.

---

## VPS Provision (Ubuntu 24.04, root)

Recommended location: **Netherlands or Germany** — lowest RTT to Telegram DCs.

```bash
# 1. Provision VPS, SSH in as root

# 2. Sync time
timedatectl set-ntp true
timedatectl status     # verify: NTP: yes, synchronized: yes

# 3. Install Python 3.11+
apt update && apt install -y python3.11 python3.11-venv git

# 4. Clone repo
git clone https://github.com/kyzaygy/telegram-gift /opt/surfsniper
cd /opt/surfsniper

# 5. Create venv and install deps
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## Configuration

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in:
- `TG_API_ID` / `TG_API_HASH` — from https://my.telegram.org
- `TG_SESSION=probe`
- `WEB_TOKEN` — generate: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `WEB_HOST=127.0.0.1`, `WEB_PORT=8080`

`targets.yaml` is pre-filled for Surge Board. Adjust only if targets change.
Leave `runtime.armed: false` until M1–M5 pass.

---

## M1 — Config loads correctly

```bash
cd /opt/surfsniper
.venv/bin/python -c "from src.config import load_config; c=load_config(); print(c)"
```

**Gate:** output shows `gift_id=5832497899283415733`, `coarse=60`, `tight=0.3`,
`tight_lead=44`. Values come from `targets.yaml`, not defaults.

---

## M2 — First login (QR + 2FA)

```bash
.venv/bin/python -m src.login
```

A QR code appears in the terminal. Open Telegram → **Settings → Devices →
Link Desktop Device** and scan it. If 2FA is enabled the script prompts for
the cloud password.

After success:

```bash
chmod 600 probe.session
```

**Gate:** `probe.session` exists in `/opt/surfsniper`. Re-running `src.login`
prints "Session already exists" and exits immediately.

---

## M3 — Main process starts without login prompt

```bash
.venv/bin/python -m src.main
```

**Gate:** process finds 2 surfs (`surfs_found count=2`), HTTP probe returns
current issue, tick logs appear. No phone/code prompt. Runs DISARMED.

If session is missing the process exits immediately:
```
client_start_failed  error=Session file not found: probe.session
Run: python -m src.login
```

---

## M4 — ABORT rule (dry-run)

```bash
.venv/bin/python -c "
import asyncio, tempfile
from unittest.mock import AsyncMock, patch
from src.config import load_config
from src.shared import AppSharedState, TargetStatus
from src.state import StateManager, SurfRecord

cfg = load_config()

async def go():
    path = tempfile.mktemp(suffix='.json')
    state = StateManager(path)
    await state.load()
    await state.register_surfs([SurfRecord(msg_id=1, gift_id=cfg.model.gift_id)])
    shared = AppSharedState(targets=[TargetStatus(target=444)], armed=True)

    issues = iter([443, 445])
    async def mock_issue(stem, tol): return next(issues)

    from src.watcher import watch_target
    with patch('src.watcher.current_issue', mock_issue), \
         patch('src.watcher.fetch_payment_form', AsyncMock(return_value=(1, object()))), \
         patch('src.watcher.fire', AsyncMock(return_value=None)):
        target = cfg.targets[0]
        try:
            await asyncio.wait_for(watch_target(None, cfg, target, 1, state, shared), timeout=5)
        except (asyncio.TimeoutError, StopIteration): pass

    print('surf status:', state.all_surfs()[0].status)
asyncio.run(go())
"
```

**Gate:** surf status becomes `aborted` (not `fired`).

---

## M5 — TL form signatures

```bash
.venv/bin/python -c "
import inspect
from pyrogram.raw import functions
send_cls = functions.payments.SendStarsForm
form_cls = functions.payments.GetPaymentForm
send_params = list(inspect.signature(send_cls.__init__).parameters.keys())[1:]
form_params = list(inspect.signature(form_cls.__init__).parameters.keys())[1:]
print('SendStarsForm  ID:', hex(getattr(send_cls, 'ID', 0)), '  params:', send_params)
print('GetPaymentForm ID:', hex(getattr(form_cls, 'ID', 0)), '  params:', form_params)
"
```

**Gate:** know the exact params of `SendStarsForm` before arming.
If `invoice` is not in `send_params`, the fallback path (form_id only) will be used.

---

## M6 — systemd deployment

```bash
# As root:
cp /opt/surfsniper/systemd/surfsniper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now surfsniper
journalctl -u surfsniper -f
```

**Gate:** service runs DISARMED, log shows `tick` events with current issue.
Survives `systemctl restart surfsniper` without login prompt.

---

## Live fire (M7)

After M1–M6 pass and star balance is confirmed (~14 780 stars):

1. Open the web dashboard and verify current issue + zone look correct.
2. Click **ARM** (or set `runtime.armed: true` and restart).
3. When issue reaches `target − 1`, the bot fires automatically.

---

## Web Dashboard

### SSH tunnel (no domain needed)

```bash
ssh -L 8080:127.0.0.1:8080 root@<vps-ip>
# Open: http://localhost:8080
```

### DuckDNS + Caddy (HTTPS from phone)

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
  gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] \
  https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
  > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

`/etc/caddy/Caddyfile`:
```
sniper.<your>.duckdns.org {
    reverse_proxy 127.0.0.1:8080
}
```

```bash
systemctl reload caddy
ufw allow OpenSSH
ufw allow 443/tcp
ufw enable
```

Dashboard requires `WEB_TOKEN` on first open (stored in sessionStorage for the tab).

**Controls:** ARM / DISARM / KILL (double-click to confirm).

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `model.slug_stem` | `SurgeBoard` | NFT slug prefix |
| `model.gift_id` | 5832497899283415733 | Telegram gift ID |
| `model.example_slug` | `SurgeBoard-1` | Documentation only |
| `probe.hole_tolerance` | 5 | Consecutive missing slugs = ceiling |
| `intervals.coarse_sec` | 60 | Poll interval when issue < `mid_at` |
| `intervals.mid_sec` | 10 | Poll interval in mid zone |
| `intervals.tight_sec` | 0.3 | Poll interval when issue ≥ target − `tight_lead` |
| `zones.mid_at` | 400 | Issue threshold for mid polling |
| `zones.tight_lead` | 44 | Distance from target that activates tight mode |
| `targets[].num` | 444 / 666 | Desired collectible number |
| `targets[].ammo_index` | 0 / 1 | Which surf to use (0-indexed) |
| `runtime.armed` | false | Initial ARM state |
| `WEB_TOKEN` | — | Dashboard auth token (empty = web disabled) |
| `WEB_HOST` | 127.0.0.1 | Bind address |
| `WEB_PORT` | 8080 | Dashboard port |

---

## Security Notes

- **Never commit** `.env`, `*.session`, `state.json`.
- One account only — multi-account spam risks a ban.
- Kill-switch: **KILL** in dashboard, `systemctl stop surfsniper`, or `Ctrl-C`.
- Single-flight: `mark_fired` written to disk only after the server confirms
  payment. On a client-side TypeError the surf is never marked used.
- Web dashboard binds to `127.0.0.1` only — expose via SSH tunnel or TLS proxy.
