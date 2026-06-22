# Surf Sniper

Maximises the probability of upgrading two «surf» star-gifts to specific
collectible numbers (444 and 666) by tracking the mint frontier and firing
at the precisely calculated moment.

> **Known unverifiable risk:** the paid upgrade path (`getPaymentForm` →
> `sendStarsForm`) cannot be tested before the release opens, and there is no
> throwaway gift to rehearse with. Diagnostics cover everything else. The first
> live shot is blind. This is a known limitation, not a bug.

---

## Architecture

```
src/
  config.py            .env + targets.yaml loader
  shared.py            In-memory state shared between bot and dashboard
  state.py             JSON persistence (fired surfs survive restarts)
  tg/client.py         Telethon session + RTT probe
  tg/gifts.py          All MTProto gift calls
  signal.py            Strategy A (availability_issued) + B (frontier probe)
  monitor.py           EMA rate estimation, adaptive poll intervals
  firecontrol.py       FSM + dynamic lead trigger
  executor.py          Single-flight lock, FLOOD_WAIT, form-expiry retry
  result.py            Parse num from Updates, HIT/MISS log
  release_detector.py  Polls can_upgrade, fires event on release
  web.py               FastAPI dashboard (runs in same asyncio loop)
  main.py              Orchestrator
  diagnostics.py       13-point pre-flight check
systemd/surfsniper.service
```

---

## VPS Provision (Debian 12 / Ubuntu 24.04)

Recommended location: **Netherlands or Germany** — lowest RTT to Telegram DCs.

```bash
# 1. Provision a fresh VPS, SSH in as root

# 2. Create dedicated user
adduser --disabled-password --gecos '' surf
mkdir -p /opt/surfsniper
chown surf:surf /opt/surfsniper

# 3. Sync time (critical for timing accuracy)
timedatectl set-ntp true
timedatectl status     # verify NTP: yes, synchronized: yes

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
# .env (chmod 600, never commit)
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in:
- `TG_API_ID` / `TG_API_HASH` — from https://my.telegram.org
- `TG_SESSION=surfsniper`
- `WEB_TOKEN` — generate: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `WEB_HOST=127.0.0.1`, `WEB_PORT=8080`

```bash
# targets.yaml
nano targets.yaml
```

Fill in:
- `model.slug_stem` — e.g. `SurfStar` (the part before the `-` in the slug)
- `model.example_slug` — e.g. `SurfStar-12` (any already-minted surf slug)

---

## First Login (creates session file)

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg.client import create_client
cfg = load_config()
asyncio.run(create_client(cfg))
print('Session OK')
"
```

Telethon prompts for phone number and confirmation code.
Session is saved as `surfsniper.session`.  `chmod 600 surfsniper.session`.

---

## Pre-flight Diagnostics

Run this before anything else:

```bash
.venv/bin/python -m src.diagnostics
```

Expected output (pre-release):

```
  [ 1] Session             ✓ PASS   user=@yourname
  [ 2] TL layer (M1)       ✓ PASS   num=12, issued=456, gift_id=789
  [ 3] RTT to DC           ✓ PASS   median=43ms (min=41 max=49, n=10)
  [ 4] Inventory (M3)      ✓ PASS   [0] msg_id=... stars=250 prepaid=no
  [ 5] Star balance        ✓ PASS   750 ★  (need 500 for 2 surfs)
  [ 6] Release detector    ✓ PASS   can_upgrade=false (pre-release — expected)
  [ 7] Payment form        ⏸ DEFERRED  error: PAYMENT_UNSUPPORTED — expected pre-release
  [ 8] parse_num (unit)    ✓ PASS
  ...
  [13] form expiry (unit)  ✓ PASS

RESULT: 12 PASS  1 DEFERRED  0 FAIL
```

**If M1 fails** (unknown constructor): switch to Kurigram — see section below.

---

## Milestones (run in order)

### M4 — Dry-run

`targets.yaml` has `dry_run: true` by default.

```bash
.venv/bin/python -m src.main
```

Watch for `DRY_RUN_would_fire` log events. The bot simulates the full loop
without touching your surfs.  Verify `issued`, `lead`, `fire_threshold` look correct.
`Ctrl-C` to stop.

### M5 — Live fire

> **Only after M1–M4 pass AND you have confirmed star balance.**

1. Set `dry_run: false` in `targets.yaml`.
2. Start the bot.  Use the dashboard to ARM when you're ready.

```bash
.venv/bin/python -m src.main
```

The dashboard **DISARMS** on startup — the bot will not fire until you press
**ARM** in the web UI (or omit WEB_TOKEN to skip the gate entirely and rely
only on `dry_run`).

---

## Web Dashboard

Access from your phone via one of two methods:

### Option 1 — SSH tunnel (no domain needed)

```bash
# On your phone (Termius or similar):
ssh -L 8080:127.0.0.1:8080 surf@<vps-ip>
# Then open: http://localhost:8080
```

### Option 2 — DuckDNS + Caddy (permanent HTTPS URL)

```bash
# Install Caddy
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
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
# Then open: https://sniper.<your>.duckdns.org
```

Firewall:
```bash
ufw allow OpenSSH
ufw allow 443/tcp
ufw enable
```

The dashboard requires the `WEB_TOKEN` on first open (stored in browser localStorage).

**Controls:**
- **ARM** — allow the bot to fire when the trigger condition is met
- **DISARM** — suppress firing (bot still tracks, does not fire)
- **KILL** — stop the bot entirely (requires double-click confirmation)

---

## systemd Deployment (M6)

```bash
# As root:
cp /opt/surfsniper/systemd/surfsniper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now surfsniper
journalctl -u surfsniper -f
```

The service reads `.env` via `EnvironmentFile`.  `state.json` survives
restarts — a surf that fired will never fire again.

---

## Kurigram Fallback (if M1 fails)

If Telethon's TL layer doesn't recognise the gift constructors:

```bash
.venv/bin/pip uninstall telethon -y
.venv/bin/pip install kurigram
```

Replace `from telethon …` imports in `src/tg/client.py` and `src/tg/gifts.py`
with the Pyrogram-style equivalents.  Everything else (monitor, firecontrol,
executor, result, state, web) is library-agnostic.

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `poll.coarse_sec` | 45 | Poll interval when distance > 50 |
| `poll.approach_sec` | 3 | Poll interval when 10 < distance ≤ 50 |
| `poll.armed_sec` | 0.3 | Poll interval when distance ≤ 10 |
| `firecontrol.safety` | 1 | Extra lead buffer |
| `firecontrol.bracket` | false | Fire both surfs around target (burns ammo) |
| `model.slug_stem` | — | e.g. `SurfStar` |
| `model.example_slug` | — | e.g. `SurfStar-12` |
| `targets[].num` | — | Desired collectible number |
| `targets[].ammo_index` | — | Which surf (0-indexed) to use |
| `runtime.dry_run` | true | `false` enables live fire |
| `WEB_TOKEN` | — | Dashboard auth token (empty = web disabled) |
| `WEB_HOST` | 127.0.0.1 | Bind address (never expose raw to internet) |
| `WEB_PORT` | 8080 | Dashboard port |

---

## Security Notes

- **Never commit** `.env`, `*.session`, or `state.json`.
- One account only — multi-account request spam risks a ban.
- Kill-switch: dashboard **KILL** button, `systemctl stop surfsniper`, or `Ctrl-C`.
- Single-flight lock in `executor.py` prevents a surf from firing twice even
  on process crash mid-shot.
- Web dashboard binds to `127.0.0.1` only; expose via SSH tunnel or TLS
  reverse-proxy — never raw to the internet.
