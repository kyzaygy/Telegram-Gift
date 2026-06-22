# Surf Sniper

Monitors the live mint counter for **Surge Board** collectible gifts via HTTP
probing and upgrades two surfs to target numbers **444** and **666** at the
precisely right moment.

> **Known unverifiable risks:**
> - The paid upgrade path (`SendStarsForm`) cannot be tested before firing —
>   the signature is confirmed by introspection (M5) but actual payment only
>   on the real shot. This is a known limitation, not a bug.
> - HTTP probing is not instantaneous. At high mint rates a rapid burst could
>   cause a miss; the surf is **not consumed** in that case (ABORT rule).
> - `availability_issued` in the Telegram NFT page HTML is frozen at mint time —
>   it is not a live counter and is not used here.

---

## Architecture

```
src/
  config.py          .env + targets.yaml loader
  state.py           JSON persistence (fired surfs survive restarts)
  tg.py              Kurigram Client + get_msg_ids + read_gift_num
  issued_probe.py    HTTP binary search → current_issue()
  watcher.py         Adaptive polling loop + fire trigger
  fire.py            Final check, payment form, SendStarsForm, result parse
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

## VPS Provision (Debian 12 / Ubuntu 24.04)

Recommended location: **Netherlands or Germany** — lowest RTT to Telegram DCs.

```bash
# 1. Provision VPS, SSH in as root

# 2. Create dedicated user
adduser --disabled-password --gecos '' surf
mkdir -p /opt/surfsniper
chown surf:surf /opt/surfsniper

# 3. Sync time
timedatectl set-ntp true
timedatectl status     # verify: NTP: yes, synchronized: yes

# 4. Install Python 3.11+
apt update && apt install -y python3.11 python3.11-venv git

# 5. Switch to service user
su - surf
cd /opt/surfsniper

# 6. Clone repo
git clone https://github.com/kyzaygy/telegram-gift .

# 7. Create venv and install deps
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

`targets.yaml` is pre-filled for Surge Board. Adjust if needed.
Leave `runtime.armed: false` until M1–M5 pass.

---

## M1 — Kurigram session + surf inventory

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg import create_client, get_msg_ids
cfg = load_config()
async def go():
    app = await create_client(cfg)
    ids = await get_msg_ids(app, cfg.model.gift_id)
    print('msg_ids:', ids)
    await app.stop()
asyncio.run(go())
"
```

Kurigram prompts for phone number and QR code or confirmation code.
Session saved as `probe.session`.  `chmod 600 probe.session`.

**Gate:** output shows exactly 2 msg_ids for the Surge Board gift.

---

## M2 — HTTP probe

```bash
.venv/bin/python -c "
import asyncio
from src.issued_probe import current_issue
result = asyncio.run(current_issue('SurgeBoard', hole_tolerance=5))
print('current_issue =', result)
"
```

**Gate:** returns `2` (SurgeBoard-1 and SurgeBoard-2 exist, SurgeBoard-3+ does not).

---

## M3 — Adaptive interval smoke test

```bash
.venv/bin/python -c "
from src.config import load_config
from src.watcher import _zone, _interval
cfg = load_config()
for issue in [395, 410, 441, 660, 663]:
    z = _zone(issue, 444, cfg)
    iv = _interval(z, cfg)
    print(f'issue={issue}  zone={z}  interval={iv}s')
"
```

**Gate:** 395→coarse/60s, 410→mid/10s, 441→tight/1.5s.

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

    # Simulate: 443 (trigger), then 445 (overshoot before fire completes)
    issues = iter([443, 445])
    async def mock_issue(stem, tol): return next(issues)

    from src.watcher import watch_target
    with patch('src.watcher.current_issue', mock_issue), \
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

---

## Live fire (M7)

After M1–M6 pass and star balance is confirmed:

1. Open the web dashboard and verify current issue + zone look correct.
2. Click **ARM** (or set `runtime.armed: true` and restart).
3. When issue reaches `target − 1`, the bot fires automatically.

---

## Web Dashboard

### SSH tunnel (no domain needed)

```bash
ssh -L 8080:127.0.0.1:8080 surf@<vps-ip>
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

Dashboard requires `WEB_TOKEN` on first open (stored in localStorage).

**Controls:** ARM / DISARM / KILL (double-click to confirm).

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `model.slug_stem` | `SurgeBoard` | NFT slug prefix |
| `model.gift_id` | 5832497899283415733 | Telegram gift ID |
| `probe.hole_tolerance` | 5 | Consecutive missing slugs = ceiling |
| `intervals.coarse_sec` | 60 | Poll interval when issue < `mid_at` |
| `intervals.mid_sec` | 10 | Poll interval in mid zone |
| `intervals.tight_sec` | 1.5 | Poll interval when issue ≥ target − `tight_lead` |
| `zones.mid_at` | 400 | Issue threshold for mid polling |
| `zones.tight_lead` | 4 | Distance from target that activates tight mode |
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
- Single-flight: `mark_fired` written to disk before RPC; on crash the surf
  is protected from double-fire on restart. If the RPC itself fails,
  `unmark_fired` rolls back the status.
- Web dashboard binds to `127.0.0.1` only — expose via SSH tunnel or TLS proxy.
