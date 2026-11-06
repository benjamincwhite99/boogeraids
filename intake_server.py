#!/usr/bin/env python3
"""Local, one-screen workflow for checking an export and rebuilding the dashboard."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from preflight import inspect

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
PUBLIC_DASHBOARD = HERE / "live-dashboard" / "dist" / "index.html"
PUBLISH_LOCK = threading.Lock()
MAX_UPLOAD = 100 * 1024 * 1024

PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Update the EveryMind dashboard</title><style>
:root{--ink:#1d2930;--muted:#66757c;--paper:#f7f8f7;--surface:#fff;--line:#dce3e1;--good:#176b4b;--warn:#9a5b08;--bad:#a63d32}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,sans-serif}.wrap{width:min(860px,calc(100% - 32px));margin:auto;padding:64px 0}h1{font-size:clamp(32px,6vw,54px);line-height:1.02;letter-spacing:-.045em;margin:0 0 14px}p{max-width:65ch}.lead{color:var(--muted);font-size:18px}.drop{display:block;margin:34px 0 18px;border:2px dashed #9eaeab;border-radius:20px;padding:42px 24px;text-align:center;background:var(--surface);transition:background .18s,border-color .18s}.drop.drag{background:#edf6f1;border-color:var(--good)}button,.open{appearance:none;border:0;border-radius:999px;background:var(--ink);color:white;font:inherit;font-weight:700;padding:12px 18px;cursor:pointer;text-decoration:none;display:inline-block}.open{background:var(--good)}.drop input{display:none}.hint{display:block;color:var(--muted);font-size:14px;margin-top:10px}.report{display:none;margin-top:26px;background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:24px}.report.show{display:block}.state{display:inline-block;padding:5px 10px;border-radius:99px;font-weight:750;font-size:13px}.ready{background:#def3e7;color:var(--good)}.blocked{background:#f9e3df;color:var(--bad)}h2{margin:14px 0 4px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:20px 0}.metric{border:1px solid var(--line);border-radius:14px;padding:13px}.metric b{display:block;font-size:27px}.metric span,li{color:var(--muted)}details{border-top:1px solid var(--line);padding:14px 0}summary{font-weight:700;cursor:pointer}.err{color:var(--bad);font-weight:600}.warn{color:var(--warn)}code{font-size:.9em}.actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:22px}.working{display:flex;align-items:center;gap:10px;color:var(--muted)}.working:before{content:"";width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--good);border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:600px){.wrap{padding:36px 0}.drop{padding:32px 18px}}
</style></head><body><main class="wrap"><h1>Update the EveryMind dashboard.</h1><p class="lead">Drop in a Microsoft Forms <code>.xlsx</code> or <code>.csv</code> export. The file stays on this computer. We check it first, then rebuild every chart with one click.</p><label class="drop" id="drop"><input id="file" type="file" accept=".xlsx,.csv"><button type="button" id="choose">Choose export</button><span class="hint">or drag a file here · nothing is uploaded beyond this computer</span></label><section class="report" id="report" aria-live="polite"></section></main><script>
const drop=document.querySelector('#drop'),input=document.querySelector('#file'),report=document.querySelector('#report');document.querySelector('#choose').onclick=()=>input.click();['dragenter','dragover'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add('drag')}));['dragleave','drop'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove('drag')}));drop.addEventListener('drop',e=>check(e.dataTransfer.files[0]));input.onchange=()=>check(input.files[0]);
function esc(x){return String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}function list(xs,cls=''){return xs.length?'<ul class="'+cls+'">'+xs.map(x=>'<li>'+esc(typeof x==='string'?x:(x.field+': '+x.message+(x.matches.length?' ('+x.matches.join(', ')+')':'')))+'</li>').join('')+'</ul>':'<p>None.</p>'}
async function check(file){if(!file)return;report.className='report show';report.innerHTML='<p>Checking <b>'+esc(file.name)+'</b>…</p>';try{let r=await fetch('/api/preflight',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file}),d=await r.json(),status=d.ready?'ready':'blocked';report.innerHTML='<span class="state '+status+'">'+(d.ready?'Ready to update':'Needs attention')+'</span><h2>'+esc(d.input)+'</h2><div class="grid"><div class="metric"><b>'+(d.rows??0).toLocaleString()+'</b><span>responses</span></div><div class="metric"><b>'+Object.keys(d.columns||{}).length+'</b><span>required columns matched</span></div><div class="metric"><b>'+(d.extra_columns||[]).length+'</b><span>extra columns</span></div></div>'+(d.errors?.length?'<h3>Blocking issues</h3>'+list(d.errors,'err'):'')+(d.warnings?.length?'<h3>Review notes</h3>'+list(d.warnings,'warn'):'')+'<details><summary>Column mapping</summary>'+list(Object.entries(d.columns||{}).map(([k,v])=>k+' → '+v))+'</details><details><summary>Unmapped values</summary>'+list((d.unmapped_values||[]).map(x=>x.column+': '+x.raw_value+' ×'+x.rows))+'</details>'+(d.ready?'<div class="actions"><button type="button" id="rebuild">Update all charts</button><span class="hint">Usually takes under a minute. Keep this window open.</span></div>':'');if(d.ready)document.querySelector('#rebuild').onclick=()=>rebuild(file)}catch(e){report.innerHTML='<p class="err">The file could not be checked: '+esc(e.message)+'</p>'}}
async function rebuild(file){report.innerHTML='<p class="working">Validating the export and rebuilding every chart…</p>';try{let r=await fetch('/api/rebuild',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file}),d=await r.json();if(!r.ok)throw new Error(d.error||'The update did not finish.');report.innerHTML='<span class="state ready">Dashboard updated</span><h2>'+esc(d.input)+'</h2><p>'+Number(d.rows).toLocaleString()+' responses passed the full cleaning and validation pipeline.</p><div class="actions"><a class="open" href="/dashboard" target="_blank">Open refreshed dashboard</a><button type="button" id="another">Use another export</button></div>';document.querySelector('#another').onclick=()=>{input.value='';report.className='report'}}catch(e){report.innerHTML='<span class="state blocked">Update stopped safely</span><p class="err">'+esc(e.message)+'</p><p>The previous dashboard is still available. Choose the file again after addressing the issue.</p>'}}
</script></body></html>'''


def receive_upload(handler: BaseHTTPRequestHandler) -> tuple[Path | None, str, str | None]:
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return None, "upload", "The upload size is invalid."
    name = Path(unquote(handler.headers.get("X-Filename", "survey_export"))).name
    suffix = Path(name).suffix.lower()
    if size <= 0 or size > MAX_UPLOAD:
        return None, name, "Choose a non-empty file smaller than 100 MB."
    if suffix not in {".xlsx", ".csv"}:
        return None, name, "Choose a .xlsx or .csv export."
    folder = Path(tempfile.mkdtemp(prefix="dashboard_upload_"))
    path = folder / name
    data = handler.rfile.read(size)
    if len(data) != size:
        shutil.rmtree(folder, ignore_errors=True)
        return None, name, "The file transfer was interrupted. Choose the export again."
    path.write_bytes(data)
    return path, name, None


def rebuild(path: Path) -> tuple[bool, str]:
    """Run the checked pipeline, restoring the prior output if any step fails."""
    with tempfile.TemporaryDirectory(prefix="dashboard_publish_") as folder:
        backup = Path(folder) / "output-backup"
        had_output = OUTPUT.exists()
        if had_output:
            shutil.copytree(OUTPUT, backup)

        def restore_previous():
            if OUTPUT.exists():
                shutil.rmtree(OUTPUT)
            if had_output:
                shutil.copytree(backup, OUTPUT)

        try:
            result = subprocess.run([sys.executable, str(HERE / "run_all.py"), str(path)], cwd=HERE,
                                    capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            restore_previous()
            return False, "The update exceeded 10 minutes and was stopped."
        if result.returncode:
            restore_previous()
            detail = (result.stderr or result.stdout or "Pipeline failed.").strip().splitlines()[-1]
            return False, detail
    PUBLIC_DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OUTPUT / "dashboard.html", PUBLIC_DASHBOARD)
    return True, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_bytes(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self.send_bytes(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/dashboard" and (OUTPUT / "dashboard.html").exists():
            self.send_bytes(200, (OUTPUT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in {"/api/preflight", "/api/rebuild"}:
            self.send_error(404)
            return
        path, name, error = receive_upload(self)
        if error:
            self.send_json(400, {"ready": False, "input": name, "errors": [error], "warnings": [], "error": error})
            return
        try:
            report = inspect(path)
            report["input"] = name
            if self.path == "/api/preflight":
                self.send_json(200, report)
                return
            if not report["ready"]:
                self.send_json(400, {**report, "error": "This export did not pass the intake check."})
                return
            if not PUBLISH_LOCK.acquire(blocking=False):
                self.send_json(409, {"error": "Another dashboard update is already running."})
                return
            try:
                ok, detail = rebuild(path)
            finally:
                PUBLISH_LOCK.release()
            if not ok:
                self.send_json(500, {"error": f"The validated update stopped: {detail}"})
                return
            self.send_json(200, {"input": name, "rows": report["rows"], "dashboard": "/dashboard"})
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "The update exceeded 10 minutes and was stopped."})
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open the update screen in the default browser")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Dashboard updater: {url} (Ctrl-C to stop)")
    if args.open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard updater closed.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
