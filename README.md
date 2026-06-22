# Surf Sniper

Maximises the probability of upgrading two «surf» star-gifts to specific
collectible numbers (444 and 666) by tracking the mint frontier and firing
at the precisely calculated moment.

---

## Prerequisites

- Python 3.11+
- A VPS **outside Russia** — Netherlands or Germany recommended (low RTT to
  Telegram DCs → less lead → better accuracy)
- NTP sync enabled: `timedatectl set-ntp true`
- `api_id` + `api_hash` from <https://my.telegram.org>
- The slug of any already-minted collectible surf (e.g. `SurfStar-12`)

---

## Installation

```bash
# 1. Clone / copy the repo onto your VPS
git clone https://github.com/kyzaygy/telegram-gift /opt/surfsniper
cd /opt/surfsniper

# 2. Create virtualenv and install dependencies
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Create .env (keep it out of git, chmod 600)
cp .env.example .env
chmod 600 .env
nano .env          # fill TG_API_ID, TG_API_HASH, TG_SESSION

# 4. Fill targets.yaml
#    - model.slug_stem    e.g.  SurfStar
#    - model.example_slug e.g.  SurfStar-12
nano targets.yaml
```

---

## First login (creates the session file)

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg.client import create_client
cfg = load_config()
asyncio.run(create_client(cfg))
print('Login OK')
"
```

Telethon will prompt for your phone number and the confirmation code.
The session file (`surfsniper.session`) is written in the current directory.

---

## Milestone checklist (run in order before going live)

### M1 — TL layer compatibility

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg.client import create_client
from src.main import run_m1
cfg = load_config()
async def _():
    c = await create_client(cfg)
    await run_m1(c, cfg.model.example_slug)
    await c.disconnect()
asyncio.run(_())
"
```

**Pass:** fields `num`, `availability_issued`, `availability_total`, `slug`,
`gift_id` are all printed without error.

**Fail / unknown constructor:** Telethon's TL layer predates the gift schema.
Switch to **Kurigram** (see section below).

---

### M2 — Signal resolver

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg.client import create_client
from src.signal import SignalResolver
cfg = load_config()
async def _():
    c = await create_client(cfg)
    sig = SignalResolver(c, cfg.model.example_slug, cfg.model.slug_stem, cfg.model.initial_frontier())
    frontier = await sig.initialize()
    print('Initial frontier:', frontier)
    live = await sig.test_strategy_a()
    print('Strategy A live:', live)
    adv = await sig.get_current_issued()
    print('Strategy B current:', adv)
    await c.disconnect()
asyncio.run(_())
"
```

**Pass:** `get_current_issued()` returns a number that advances over time.

---

### M3 — Inventory

```bash
.venv/bin/python -c "
import asyncio
from src.config import load_config
from src.tg.client import create_client
from src.main import run_m1, run_m3
cfg = load_config()
async def _():
    c = await create_client(cfg)
    gid = await run_m1(c, cfg.model.example_slug)
    surfs = await run_m3(c, gid, cfg)
    for i, s in enumerate(surfs):
        print(f'  [{i}] msg_id={s.msg_id}  stars={s.upgrade_stars}  prepaid={s.is_prepaid}')
    await c.disconnect()
asyncio.run(_())
"
```

**Pass:** two surfs are listed with their `msg_id` values.

---

### M4 — Dry-run (default)

`targets.yaml` already has `dry_run: true`. Run the full bot:

```bash
.venv/bin/python -m src.main
```

Watch the logs. You should see `DRY_RUN_would_fire` events when the trigger
condition is satisfied. Verify the `issued` and `lead` values look correct.
Kill with `Ctrl-C` — no gifts are consumed.

---

### M5 — Live fire

> **Only proceed after M1–M4 all pass.**

1. Set `dry_run: false` in `targets.yaml`.
2. Confirm star balance covers `upgrade_stars × 2`.
3. Start the bot:

```bash
.venv/bin/python -m src.main
```

After each shot `state.json` is updated. The log will print `HIT` or `MISS`
with the actual `num` received.

---

## systemd deployment (M6)

```bash
useradd -r -s /sbin/nologin surfsniper
cp systemd/surfsniper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now surfsniper
journalctl -u surfsniper -f
```

Ensure `.env`, `*.session`, and `state.json` are readable only by the
service user (`chmod 600`, `chown surfsniper:`).

---

## Kurigram fallback (if M1 fails)

If Telethon's layer doesn't know the gift constructors:

```bash
.venv/bin/pip uninstall telethon -y
.venv/bin/pip install kurigram
```

Then replace `from telethon …` imports in `src/tg/client.py` and
`src/tg/gifts.py` with the Pyrogram-style equivalents. The rest of the
codebase (monitor, firecontrol, executor, result, state) is library-agnostic.

---

## Configuration reference

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

---

## Security notes

- **Never commit** `.env`, `*.session`, or `state.json`.
- One account only — multi-account spam is a ban vector.
- Kill-switch: `systemctl stop surfsniper` or `Ctrl-C`.
- The single-flight lock in `executor.py` prevents a surf from firing twice
  even if the process crashes mid-shot.