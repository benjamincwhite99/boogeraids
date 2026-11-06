#!/usr/bin/env python3
"""HTTP checks for the local drag-and-drop dashboard updater."""
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import intake_server as app  # noqa: E402
from test_edge_cases import row, write_xlsx  # noqa: E402


def post(url, path):
    request = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="POST",
        headers={"X-Filename": path.name, "Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def main():
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    original_rebuild = app.rebuild
    try:
        with urllib.request.urlopen(root + "/") as response:
            page = response.read().decode()
        assert response.status == 200 and "Update all charts" in page and "drag" in page

        with tempfile.TemporaryDirectory(prefix="intake_server_test_") as folder:
            export = Path(folder) / "new export.xlsx"
            write_xlsx([row(1)], export)
            status, report = post(root + "/api/preflight", export)
            assert status == 200 and report["ready"] and report["rows"] == 1, report

            app.rebuild = lambda _: (True, "")
            status, result = post(root + "/api/rebuild", export)
            assert status == 200 and result["rows"] == 1 and result["dashboard"] == "/dashboard", result
    finally:
        app.rebuild = original_rebuild
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("3/3 dashboard updater tests passed")


if __name__ == "__main__":
    main()
