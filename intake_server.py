#!/usr/bin/env python3
"""Run a local drag-and-drop preflight screen for new survey exports.

    python3 intake_server.py
    # then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from preflight import inspect

HERE = Path(__file__).resolve().parent

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Survey intake check</title><style>
:root{--ink:#1d2930;--muted:#66757c;--paper:#f7f8f7;--surface:#fff;--line:#dce3e1;--good:#176b4b;--warn:#9a5b08;--bad:#a63d32}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,sans-serif}.wrap{width:min(860px,calc(100% - 32px));margin:auto;padding:64px 0}h1{font-size:clamp(32px,6vw,54px);line-height:1.02;letter-spacing:-.045em;margin:0 0 14px}p{max-width:65ch}.lead{color:var(--muted);font-size:18px}.drop{margin:34px 0 18px;border:2px dashed #9eaeab;border-radius:20px;padding:42px 24px;text-align:center;background:var(--surface);transition:background .18s,border-color .18s}.drop.drag{background:#edf6f1;border-color:var(--good)}button{appearance:none;border:0;border-radius:999px;background:var(--ink);color:white;font:inherit;font-weight:700;padding:12px 18px;cursor:pointer}.drop input{display:none}.hint{display:block;color:var(--muted);font-size:14px;margin-top:10px}.report{display:none;margin-top:26px;background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:24px}.report.show{display:block}.state{display:inline-block;padding:5px 10px;border-radius:99px;font-weight:750;font-size:13px}.ready{background:#def3e7;color:var(--good)}.blocked{background:#f9e3df;color:var(--bad)}h2{margin:14px 0 4px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:20px 0}.metric{border:1px solid var(--line);border-radius:14px;padding:13px}.metric b{display:block;font-size:27px}.metric span,li{color:var(--muted)}details{border-top:1px solid var(--line);padding:14px 0}summary{font-weight:700;cursor:pointer}.err{color:var(--bad);font-weight:600}.warn{color:var(--warn)}code{font-size:.9em}@media(max-width:600px){.wrap{padding:36px 0}.drop{padding:32px 18px}}
</style></head><body><main class="wrap"><h1>Check a new survey export before it changes anything.</h1><p class="lead">Drop in a Microsoft Forms <code>.xlsx</code> or <code>.csv</code> export. This check runs locally, never publishes output, and tells you whether the current data contract can process it.</p><label class="drop" id="drop"><input id="file" type="file" accept=".xlsx,.csv"><button type="button" id="choose">Choose export</button><span class="hint">or drag a file here · nothing is uploaded beyond this computer</span></label><section class="report" id="report" aria-live="polite"></section></main><script>
const drop=document.querySelector('#drop'),input=document.querySelector('#file'),report=document.querySelector('#report');document.querySelector('#choose').onclick=()=>input.click();['dragenter','dragover'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add('drag')}));['dragleave','drop'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove('drag')}));drop.addEventListener('drop',e=>send(e.dataTransfer.files[0]));input.onchange=()=>send(input.files[0]);
function esc(x){return String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}function list(xs,cls=''){return xs.length?'<ul class="'+cls+'">'+xs.map(x=>'<li>'+esc(typeof x==='string'?x:(x.field+': '+x.message+(x.matches.length?' ('+x.matches.join(', ')+')':'')))+'</li>').join('')+'</ul>':'<p>None.</p>'}
async function send(file){if(!file)return;report.className='report show';report.innerHTML='<p>Checking <b>'+esc(file.name)+'</b>…</p>';try{let r=await fetch('/api/preflight',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});let d=await r.json();let status=d.ready?'ready':'blocked';report.innerHTML='<span class="state '+status+'">'+(d.ready?'Ready to process':'Needs attention')+'</span><h2>'+esc(d.input)+'</h2><div class="grid"><div class="metric"><b>'+(d.rows??0).toLocaleString()+'</b><span>responses</span></div><div class="metric"><b>'+Object.keys(d.columns||{}).length+'</b><span>required columns matched</span></div><div class="metric"><b>'+(d.extra_columns||[]).length+'</b><span>extra columns</span></div></div>'+(d.errors?.length?'<h3>Blocking issues</h3>'+list(d.errors,'err'):'')+(d.warnings?.length?'<h3>Review notes</h3>'+list(d.warnings,'warn'):'')+'<details><summary>Column mapping</summary>'+list(Object.entries(d.columns||{}).map(([k,v])=>k+' → '+v))+'</details><details><summary>Unmapped values</summary>'+list((d.unmapped_values||[]).map(x=>x.column+': '+x.raw_value+' ×'+x.rows))+'</details><details><summary>What happens next</summary><p>When this report is ready, run <code>python3 run_all.py "'+esc(d.input)+'"</code>. The full pipeline validates the output before replacing the dashboard.</p></details>'}catch(e){report.innerHTML='<p class="err">The file could not be checked: '+esc(e.message)+'</p>'}}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/preflight":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 100 * 1024 * 1024:
            self.send_json(400, {"ready": False, "input": "upload", "errors": ["Choose a non-empty file smaller than 100 MB."], "warnings": []})
            return
        name = Path(unquote(self.headers.get("X-Filename", "survey_export"))).name
        suffix = Path(name).suffix.lower()
        if suffix not in {".xlsx", ".csv"}:
            self.send_json(400, {"ready": False, "input": name, "errors": ["Choose a .xlsx or .csv export."], "warnings": []})
            return
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as upload:
            upload.write(self.rfile.read(size))
            path = Path(upload.name)
        try:
            result = inspect(path)
            result["input"] = name
            self.send_json(200, result)
        finally:
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Intake check: http://127.0.0.1:{args.port} (Ctrl-C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
