"""
FastAPI web dashboard — runs as an asyncio task inside the main process,
shares AppSharedState directly with the sniper loops (no IPC).

Bind: 127.0.0.1:WEB_PORT (expose externally via SSH tunnel or Caddy TLS proxy).
Auth: all endpoints require  Authorization: Bearer <WEB_TOKEN>.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from src.shared import AppSharedState

_SECURITY = HTTPBearer(auto_error=True)

# ---------------------------------------------------------------------------
# Embedded HTML — single-page dashboard, mobile-friendly, dark theme
# ---------------------------------------------------------------------------
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Surf Sniper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono','Fira Code',monospace;background:#0d1117;color:#e6edf3;font-size:13px;line-height:1.6}
.wrap{max-width:820px;margin:0 auto;padding:16px}
h1{font-size:17px;color:#58a6ff;margin-bottom:14px;display:flex;align-items:center;gap:10px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:12px}
.card h2{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#8b949e;font-weight:normal;padding:3px 6px;font-size:11px}
td{padding:3px 6px}
.IDLE,.COARSE{color:#8b949e}
.APPROACH{color:#f0883e}
.ARMED{color:#ffa657;font-weight:bold}
.FIRED{color:#a5d6ff}
.DONE{color:#56d364;font-weight:bold}
.ABORTED{color:#ff7b72;opacity:.7}
.READY{color:#e6edf3}
.LOCKED{color:#8b949e;text-decoration:line-through}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold}
.ok{background:#1a4731;color:#56d364}
.warn{background:#3d2b00;color:#f0883e}
.off{background:#21262d;color:#8b949e}
.red{background:#4d0000;color:#ff7b72}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{padding:8px 18px;border:1px solid #30363d;border-radius:6px;background:#21262d;color:#e6edf3;cursor:pointer;font-family:inherit;font-size:13px;transition:.15s}
button:hover{background:#30363d}
button.arm{background:#1f6feb;border-color:#388bfd}
button.arm:hover{background:#388bfd}
button.kill{background:#6e0700;border-color:#ff7b72;color:#ff7b72}
button.kill:hover{background:#8e0000}
.logs{background:#010409;padding:10px;border-radius:4px;height:210px;overflow-y:auto;font-size:11px;color:#8b949e}
.logs .ln{margin-bottom:1px;word-break:break-all;white-space:pre-wrap}
.meta{color:#484f58;font-size:11px;margin-top:8px}
input[type=password]{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px 12px;font-family:inherit;font-size:13px;width:100%;margin-bottom:8px}
.login{max-width:380px;margin:80px auto}
.hit{color:#56d364;font-weight:bold}
.miss{color:#ff7b72}
</style>
</head>
<body>
<div class="wrap">
<div id="login" class="login" style="display:none">
  <div class="card">
    <h2>Authentication required</h2>
    <input type="password" id="tok" placeholder="WEB_TOKEN" onkeydown="if(event.key==='Enter')auth()"/>
    <button class="arm" style="width:100%" onclick="auth()">Connect</button>
    <div id="lerr" style="color:#ff7b72;margin-top:8px"></div>
  </div>
</div>
<div id="dash" style="display:none">
  <h1>SURF SNIPER <span id="arm-badge" class="badge off">DISARMED</span></h1>
  <div class="card">
    <h2>Targets</h2>
    <div id="tgt"></div>
  </div>
  <div class="card">
    <h2>Account</h2>
    <div id="acct"></div>
  </div>
  <div class="card">
    <h2>Controls</h2>
    <div class="btns">
      <button class="arm" onclick="ctrl('arm')">ARM</button>
      <button onclick="ctrl('disarm')">DISARM</button>
      <button class="kill" onclick="kill()">KILL</button>
    </div>
    <div id="ctrl-msg" style="margin-top:8px;color:#8b949e;font-size:11px"></div>
  </div>
  <div class="card">
    <h2>Log tail</h2>
    <div class="logs" id="logs"></div>
  </div>
  <div class="meta" id="meta"></div>
</div>
</div>
<script>
let tok=localStorage.getItem('st')||'';
let killStep=0;
function hdr(){return{'Authorization':'Bearer '+tok}}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

async function auth(){
  tok=document.getElementById('tok').value.trim();
  const r=await fetch('/api/status',{headers:hdr()});
  if(r.status===401){document.getElementById('lerr').textContent='Invalid token';return}
  localStorage.setItem('st',tok);
  showDash();start()
}

async function poll(){
  try{
    const r=await fetch('/api/status',{headers:hdr()});
    if(r.status===401){showLogin();return}
    render(await r.json())
  }catch(e){document.getElementById('meta').textContent='Error: '+e.message}
}

async function pollLogs(){
  try{
    const r=await fetch('/api/logs',{headers:hdr()});
    if(!r.ok)return;
    const lines=await r.json();
    const el=document.getElementById('logs');
    el.innerHTML=lines.map(l=>'<div class="ln">'+esc(l)+'</div>').join('');
    el.scrollTop=el.scrollHeight
  }catch(e){}
}

function render(d){
  // Targets table
  let h='<table><tr><th>Target</th><th>FSM</th><th>Issued</th><th>Dist</th><th>Rate /s</th><th>Lead</th><th>Surf</th><th>Result</th></tr>';
  for(const s of d.snipers){
    const res=s.result_num!==null?(s.result_num===s.target?'<span class="hit">#'+s.result_num+' HIT</span>':'<span class="miss">#'+s.result_num+' MISS</span>'):'—';
    h+=`<tr><td>#${s.target}</td><td class="${s.state}">${s.state}</td><td>${s.issued}</td><td>${s.distance}</td><td>${s.rate.toFixed(3)}</td><td>${s.lead}</td><td class="${s.surf_status}">${s.surf_status}</td><td>${res}</td></tr>`
  }
  document.getElementById('tgt').innerHTML=h+'</table>';

  // Account
  const ub=d.upgrade_open?'<span class="badge ok">OPEN</span>':'<span class="badge off">CLOSED</span>';
  const bal=d.star_balance!==null?d.star_balance+' ★':'—';
  const lp=d.last_poll_at>0?new Date(d.last_poll_at*1e3).toLocaleTimeString():'—';
  document.getElementById('acct').innerHTML=`
<table>
<tr><th>Session</th><td>${d.session_valid?'<span class="badge ok">valid</span>':'<span class="badge red">INVALID</span>'}</td></tr>
<tr><th>RTT</th><td>${d.rtt_ms.toFixed(1)} ms</td></tr>
<tr><th>Stars</th><td>${bal}</td></tr>
<tr><th>Upgrade</th><td>${ub}</td></tr>
<tr><th>Last release poll</th><td>${lp}</td></tr>
</table>`;

  // Armed badge
  const b=document.getElementById('arm-badge');
  if(d.armed){b.className='badge ok';b.textContent='ARMED'}
  else{b.className='badge off';b.textContent='DISARMED'}

  document.getElementById('meta').textContent='Updated '+new Date().toLocaleTimeString()
}

async function ctrl(action){
  const r=await fetch('/api/'+action,{method:'POST',headers:hdr()});
  const j=await r.json();
  document.getElementById('ctrl-msg').textContent=j.ok||j.error||'';
  poll()
}

async function kill(){
  if(killStep===0){
    killStep=1;
    document.getElementById('ctrl-msg').textContent='⚠ Click KILL again to confirm shutdown.';
    setTimeout(()=>{killStep=0;document.getElementById('ctrl-msg').textContent=''},6e3);
    return
  }
  killStep=0;
  await fetch('/api/kill',{method:'POST',headers:hdr()});
  document.getElementById('ctrl-msg').textContent='Kill signal sent. Bot stopping.';
}

function showLogin(){
  document.getElementById('login').style.display='';
  document.getElementById('dash').style.display='none'
}
function showDash(){
  document.getElementById('login').style.display='none';
  document.getElementById('dash').style.display=''
}
function start(){poll();pollLogs();setInterval(poll,1500);setInterval(pollLogs,3000)}

tok?( showDash(), start() ):showLogin()
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_web_app(shared: "AppSharedState", token: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    def _verify(creds: HTTPAuthorizationCredentials = Depends(_SECURITY)) -> None:
        if not token or creds.credentials != token:
            raise HTTPException(status_code=401, detail="Invalid token")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_HTML)

    @app.get("/api/status", dependencies=[Depends(_verify)])
    async def status() -> JSONResponse:
        return JSONResponse({
            "ts": time.time(),
            "snipers": [
                {
                    "target": s.target,
                    "state": s.state,
                    "issued": s.issued,
                    "distance": s.distance,
                    "rate": s.rate,
                    "lead": s.lead,
                    "surf_msg_id": s.surf_msg_id,
                    "surf_status": s.surf_status,
                    "result_num": s.result_num,
                }
                for s in shared.snipers
            ],
            "upgrade_open": shared.upgrade_open,
            "last_poll_at": shared.last_poll_at,
            "star_balance": shared.star_balance,
            "session_valid": shared.session_valid,
            "rtt_ms": shared.rtt_ms,
            "armed": shared.armed,
        })

    @app.get("/api/logs", dependencies=[Depends(_verify)])
    async def logs() -> JSONResponse:
        return JSONResponse(list(shared.log_tail))

    @app.post("/api/arm", dependencies=[Depends(_verify)])
    async def arm() -> JSONResponse:
        shared.armed = True
        return JSONResponse({"ok": "Armed — bot will fire when trigger conditions are met."})

    @app.post("/api/disarm", dependencies=[Depends(_verify)])
    async def disarm() -> JSONResponse:
        shared.armed = False
        return JSONResponse({"ok": "Disarmed — bot will not fire even if trigger fires."})

    @app.post("/api/kill", dependencies=[Depends(_verify)])
    async def kill() -> JSONResponse:
        shared.armed = False
        shared.kill_requested = True
        return JSONResponse({"ok": "Kill signal sent."})

    return app


async def start_web_server(
    shared: "AppSharedState",
    token: str,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> asyncio.Task:
    app = create_web_app(shared, token)
    uvi_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_config)
    task = asyncio.create_task(server.serve(), name="web")
    return task
