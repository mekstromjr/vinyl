"""The agent: a JSON control surface, plus an optional kiosk page over it.

Two things live here, and the split matters. The endpoints (/status, /scan,
/arm, /plug, /stop, /cancel, /ack) are the station's actual interface and are
always served. The kiosk HTML is one constant rendered at "/", and it is only a
client of those endpoints — it can be switched off (VINYL_SERVE_KIOSK=0)
without touching what the station can do.

It stays ON by default: every station today has an operator standing at it, and
the local page is the deliberate offline fallback for when the cluster is
unreachable (meklab/vinyl#11).

The kiosk: four screens, one page, 500 ms polling — SCAN (barcode field
autofocused, the HID scanner types into it), GRID (side status, next pending
preselected), RECORDING (meter with peak-hold + target band, countdown,
STOP/CANCEL), and REVIEW (verdict: flip it, next record, or try again).
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from vinyl_capture import ha
from vinyl_capture.apiclient import ApiError, VinylApi
from vinyl_capture.config import settings
from vinyl_capture.heartbeat import Heartbeat
from vinyl_capture.station import Station
from vinyl_capture.uploader import Uploader

_ = HTMLResponse  # used via decorator response_class


class ScanBody(BaseModel):
    code: str


class ArmBody(BaseModel):
    side_id: str


class PlugBody(BaseModel):
    state: Literal["on", "off"]


def create_app(station: Station | None = None, api: VinylApi | None = None) -> FastAPI:
    s = settings()
    api = api or VinylApi(s.api_url, s.api_key)
    station = station or Station(s, api)
    uploader = Uploader(s, api, station)
    uploader.start()

    app = FastAPI(title="vinyl-capture", docs_url=None, redoc_url=None)
    app.state.station = station
    app.state.api = api

    # Presence is optional to capturing, so a station that cannot reach the
    # cluster still records — the thread just retries in the background.
    heartbeat = Heartbeat(s, api, station, uploader=uploader)
    app.state.heartbeat = heartbeat
    heartbeat.start()

    if s.serve_kiosk:
        @app.get("/", response_class=HTMLResponse)
        def index():
            return _PAGE

    @app.get("/status")
    def status():
        return dict(station.status(), uploads=uploader.status())

    @app.post("/scan")
    def scan(body: ScanBody):
        code = body.code.strip().upper()
        try:
            acc = api.accession(code)
            sides = api.sides(code)
        except ApiError as exc:
            raise HTTPException(404, str(exc)) from exc
        # A good scan means a record is about to go on the platter: power the
        # turntable now instead of making Michael toggle the plug by hand.
        ha.plug_on(station.settings)
        release = acc.get("release") or {}
        pending = [x for x in sides if x["state"] == "pending" and x["speed_rpm"] != 78]
        return {
            "accession": {
                "accession_id": acc["accession_id"],
                "artist": release.get("artist"),
                "title": release.get("title"),
                "status": acc["status"],
            },
            "sides": sides,
            "suggested_side_id": pending[0]["side_id"] if pending else None,
            "all_78": bool(sides) and all(x["speed_rpm"] == 78 for x in sides),
        }

    @app.post("/arm")
    def arm(body: ArmBody):
        try:
            sides = api.sides(body.side_id.rsplit("-", 2)[0])
            side = next(x for x in sides if x["side_id"] == body.side_id.upper())
        except (ApiError, StopIteration) as exc:
            raise HTTPException(404, f"no such side: {body.side_id}") from exc
        try:
            station.arm(side)
        except (RuntimeError, ApiError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return station.status()

    @app.post("/plug")
    def plug(body: PlugBody):
        """Manual turntable power for setup and testing between captures."""
        if body.state == "off" and station.phase == "recording":
            raise HTTPException(409, "recording in progress — power stays on")
        (ha.plug_on if body.state == "on" else ha.plug_off)(station.settings)
        return {"ok": True}

    @app.post("/stop")
    def stop():
        try:
            return station.stop(reason="operator")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/cancel")
    def cancel():
        try:
            station.cancel()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return station.status()

    @app.post("/ack")
    def ack():
        station.acknowledge()
        return station.status()

    @app.get("/audio/{capture_id}")
    def audio(capture_id: str):
        from fastapi.responses import FileResponse

        matches = list(s.staging_dir.glob(f"*__{capture_id}.flac"))
        if not matches:
            raise HTTPException(404, "no staged audio for that capture (already uploaded?)")
        return FileResponse(matches[0], media_type="audio/flac")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "phase": station.phase}

    return app


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>vinyl capture</title>
<style>
  body { margin:0; background:#14110e; color:#f2ede6;
         font:20px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 60rem; margin:0 auto; padding:1.2rem; }
  .screen { display:none; } .screen.active { display:block; }
  h1 { font-size:1.4rem; color:#c9a97e; }
  input { font-size:2rem; padding:.7rem; width:100%; border-radius:10px; border:0; }
  button { font-size:1.6rem; font-weight:700; border:0; border-radius:14px;
           padding:1rem 1.6rem; margin:.5rem .4rem 0 0; cursor:pointer;
           background:#7a4a2b; color:#fff; }
  button.stop { background:#b3261e; font-size:2.4rem; padding:1.4rem 3rem; }
  button.ghost { background:#3a332b; }
  table { width:100%; border-collapse:collapse; font-size:1.2rem; margin-top:.8rem; }
  td,th { padding:.6rem; border-bottom:1px solid #3a332b; text-align:left; }
  tr.selectable { cursor:pointer; } tr.selected { background:#2b2117; outline:2px solid #c9a97e; }
  .tag { border-radius:99px; padding:.1rem .6rem; font-size:.85rem; font-weight:700; }
  .tag.pending { background:#4a3a1c; color:#e8c476; }
  .tag.done { background:#1d3a1d; color:#8fd48f; }
  .tag.other { background:#3a332b; color:#bbb; }
  .big { font-size:3rem; font-weight:800; }
  .meterwrap { background:#000; border-radius:10px; padding:1rem; margin:1rem 0; }
  .meter { position:relative; height:2.2rem; background:#1c1c1c; border-radius:6px;
           margin:.5rem 0; overflow:hidden; }
  .meter .zone-good { position:absolute; top:0; bottom:0; background:#1d3a1d; }
  .meter .zone-hot  { position:absolute; top:0; bottom:0; background:#4a1c1c; }
  .meter .bar { position:absolute; top:0; bottom:0; left:0; background:#c9a97e; }
  .meter .hold { position:absolute; top:0; bottom:0; width:3px; background:#fff; }
  .verdict-pass { color:#8fd48f; } .verdict-fail { color:#ff8f86; }
  .countdown { color:#8a8378; font-size:1.1rem; }
  .err { color:#ff8f86; min-height:1.4rem; }
</style></head><body><main>

<div id="uploads" style="position:fixed;bottom:0;left:0;right:0;background:#0d0b09;
     color:#8a8378;font-size:.95rem;padding:.4rem 1.2rem;display:none"></div>

<div id="scan" class="screen active">
  <h1>Scan the card (or type the accession number)</h1>
  <input id="code" autofocus autocomplete="off" placeholder="GM-0142">
  <div id="scanerr" class="err"></div>
  <div style="margin-top:2.5rem;color:#8a8378">
    Turntable power:
    <button class="ghost" id="plug-on" style="margin-left:.6rem">On</button>
    <button class="ghost" id="plug-off" style="margin-left:.4rem">Off</button>
  </div>
</div>

<div id="grid" class="screen">
  <div class="big" id="g-acc"></div>
  <div id="g-title" style="color:#c9a97e"></div>
  <div id="g-warn" class="err"></div>
  <table id="g-table"></table>
  <button id="g-go">Record selected side</button>
  <button class="ghost" onclick="showScan()">Different record</button>
</div>

<div id="rec" class="screen">
  <div class="big" id="r-side"></div>
  <div>Start the turntable, drop the needle. Recording already.</div>
  <div class="meterwrap">
    <div>L</div><div class="meter" id="m-l"></div>
    <div>R</div><div class="meter" id="m-r"></div>
    <div class="countdown">Peaks in the green band (-12 to -6 dBFS). Held white line = recent max.</div>
  </div>
  <div class="countdown" id="r-time"></div>
  <button class="stop" onclick="post('/stop')">STOP</button>
  <button class="ghost" onclick="if(confirm('Abandon this capture?')) post('/cancel')">Cancel</button>
</div>

<div id="review" class="screen">
  <div class="big" id="v-head"></div>
  <div id="v-detail" style="margin:.6rem 0"></div>
  <div id="v-listen" style="display:none">
    <canvas id="v-wave" height="90" style="width:100%;background:#000;border-radius:8px;cursor:pointer"></canvas>
    <audio id="v-audio" controls preload="metadata" style="width:100%;margin-top:.4rem"></audio>
  </div>
  <button id="v-next" onclick="post('/ack').then(showScan)">Next side / record</button>
  <button class="ghost" id="v-retry">Record this side again</button>
</div>

<script>
const $ = id => document.getElementById(id);
let selected = null, lastScan = null;

function show(id){ document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  $(id).classList.add('active');
  // Leaving the review screen must SILENCE it — a hidden <audio> keeps playing.
  if(id !== 'review'){ const a = $('v-audio');
    if(a && a.src){ a.pause(); a.removeAttribute('src'); a.load(); } } }
function showScan(){ show('scan'); $('code').value=''; $('code').focus(); $('scanerr').textContent=''; }

async function post(path, body){ const r = await fetch(path,{method:'POST',
  headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):null});
  if(!r.ok){ const d = await r.json().catch(()=>({detail:r.statusText}));
    throw new Error(d.detail || 'error'); }
  return r.json(); }

$('plug-on').addEventListener('click', () => post('/plug', {state:'on'}));
$('plug-off').addEventListener('click', () =>
  post('/plug', {state:'off'}).catch(err => { $('scanerr').textContent = err.message; }));
$('code').addEventListener('keydown', async e => {
  if(e.key !== 'Enter') return;
  const code = $('code').value.trim();
  if(!code) return;
  try {
    lastScan = await post('/scan', {code});
    renderGrid(lastScan);
  } catch(err){ $('scanerr').textContent = err.message; $('code').select(); }
});

function renderGrid(d){
  $('g-acc').textContent = d.accession.accession_id;
  $('g-title').textContent = d.accession.artist ?
    d.accession.artist + ' — ' + d.accession.title : '(release not bound yet)';
  $('g-warn').textContent = d.all_78 ? '78 RPM record — set it aside for phase two.' : '';
  selected = d.suggested_side_id;
  const rows = d.sides.map(s => {
    const tag = s.state === 'pending' ? 'pending' : (s.state.startsWith('qc')||s.state==='uploaded'||s.state==='captured' ? 'done' : 'other');
    return `<tr class="selectable ${s.side_id===selected?'selected':''}" data-side="${s.side_id}">
      <td>${s.side_id.split('-').slice(2).join('-')}</td><td>${s.speed_rpm} rpm</td>
      <td><span class="tag ${tag}">${s.state}</span></td></tr>`; }).join('');
  $('g-table').innerHTML = '<tr><th>Side</th><th>Speed</th><th>Status</th></tr>' + rows;
  document.querySelectorAll('#g-table tr.selectable').forEach(tr =>
    tr.addEventListener('click', () => { selected = tr.dataset.side; renderGrid({...lastScan, suggested_side_id:selected}); }));
  $('g-go').disabled = !selected;
  show('grid');
}

$('g-go').addEventListener('click', async () => {
  if(!selected) return;
  const s = lastScan.sides.find(x=>x.side_id===selected);
  if(s && s.state !== 'pending' && !confirm(`${selected} is already ${s.state}. Re-record it? The old master stays but stops being current.`)) return;
  try { await post('/arm', {side_id: selected}); } catch(err){ $('g-warn').textContent = err.message; }
});

function drawWave(canvas, env){
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.clientWidth;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(!env.length) { ctx.fillStyle='#666'; ctx.fillText('no waveform for this capture', 10, 20); return; }
  ctx.fillStyle = '#c9a97e';
  const w = canvas.width / env.length, h = canvas.height;
  // Linear amplitude, normalized to this side's own loudest second: dB
  // scaling made surface noise fill half the height (solid-block look).
  const peak = Math.max(...env, 1e-6);
  env.forEach((v,i) => {
    const bar = Math.max(1, (v / peak) * h);
    ctx.fillRect(i*w, (h-bar)/2, Math.max(1, w*0.8), bar);
  });
}
function pct(db){ return Math.min(100, Math.max(0, (db + 60) / 60 * 100)); }
function drawMeter(el, lin, hold){
  const db = lin>0 ? 20*Math.log10(lin) : -120, hdb = hold>0 ? 20*Math.log10(hold) : -120;
  el.innerHTML = `<div class="zone-good" style="left:${pct(-12)}%;width:${pct(-6)-pct(-12)}%"></div>` +
    `<div class="zone-hot" style="left:${pct(-3)}%;right:0"></div>` +
    `<div class="bar" style="width:${pct(db)}%"></div>` +
    `<div class="hold" style="left:${pct(hdb)}%"></div>`;
}

setInterval(async () => {
  try {
    const st = await (await fetch('/status')).json();
    const up = st.uploads || {};
    const bar = $('uploads');
    if (up.staged > 0 || up.current) {
      bar.style.display = 'block';
      let msg = up.staged + ' side' + (up.staged === 1 ? '' : 's') + ' waiting to upload';
      if (up.current) msg += ' — ' + up.current.file.split('__')[0] +
        ' part ' + up.current.part + '/' + up.current.parts_total;
      if (up.last_error && !up.current) msg += ' — last error: ' + up.last_error;
      bar.textContent = msg;
    } else { bar.style.display = 'none'; }
    if(st.phase === 'recording'){
      if(!$('rec').classList.contains('active')) show('rec');
      $('r-side').textContent = st.side ? st.side.side_id : '';
      const el = Math.floor(st.elapsed_sec||0);
      $('r-time').textContent = `${Math.floor(el/60)}:${String(el%60).padStart(2,'0')} elapsed` +
        (st.remaining_sec!=null ? ` — auto-stop in ${Math.floor(st.remaining_sec/60)}:${String(st.remaining_sec%60).padStart(2,'0')}` : '') +
        (st.meter && st.meter.clipped ? ` — CLIPPING (${st.meter.clipped})` : '');
      if(st.meter){ drawMeter($('m-l'), st.meter.l, st.meter.hold_l);
                    drawMeter($('m-r'), st.meter.r, st.meter.hold_r); }
    } else if(st.phase === 'review' && st.review){
      if(!$('review').classList.contains('active')){
        const ok = st.review.status === 'captured';
        if(ok && st.capture_id){
          $('v-listen').style.display = 'block';
          $('v-audio').src = '/audio/' + st.capture_id;
          drawWave($('v-wave'), (st.review.summary||{}).envelope_1s || []);
          $('v-wave').onclick = e => {
            const a = $('v-audio'), frac = e.offsetX / $('v-wave').clientWidth;
            if (a.duration) { a.currentTime = frac * a.duration; a.play(); }
          };
        } else { $('v-listen').style.display = 'none'; $('v-audio').removeAttribute('src'); }
        $('v-head').innerHTML = ok ? '<span class="verdict-pass">Capture OK</span>'
                                   : '<span class="verdict-fail">Capture FAILED</span>';
        $('v-detail').innerHTML = ok
          ? `${st.review.side_id} — ${Math.round(st.review.summary.duration_sec/60)} min, peaks ` +
            `${st.review.summary.peak_dbfs_l} / ${st.review.summary.peak_dbfs_r} dBFS. ` +
            `Flip the record or scan the next one. Upload happens automatically.`
          : st.review.reasons.join('<br>') + '<br>Fix the cause and record this side again.';
        $('v-retry').onclick = async () => { await post('/ack');
          if(st.review.side_id){ selected = st.review.side_id;
            try{ await post('/arm', {side_id: selected}); }catch(e){ showScan(); } } };
        show('review');
      }
    } else if(st.phase === 'idle' && ($('rec').classList.contains('active'))){
      show('review'); // stop landed; review payload arrives next poll
    }
  } catch(e) { /* poll again */ }
}, 500);
</script>
</main></body></html>
"""
