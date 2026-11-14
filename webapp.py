#!/usr/bin/env python3
"""
webapp.py - local web interface for reviewing duplicate files.

    python webapp.py              # opens http://127.0.0.1:8765 in your browser
    python webapp.py --port 9000 --no-browser

Scan a folder, see every duplicate set and where each copy lives, choose which
copies to keep, then delete the rest, move them to the 'dups' folder, or replace
them with hard links to a kept copy.
Nothing is touched until you confirm. Uses only the Python standard library.

Safety:
  * listens on 127.0.0.1 only, checks the Host header, and requires a per-run
    token on every API call, so other websites can't drive it;
  * the server only deletes paths that came from the current scan, refuses any
    request that would remove every copy of a set, and skips files whose size
    changed since the scan.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dedup_core import (APP_ROOT, DUPS_DIR, Cancelled, delete_file, find_duplicates,
                        format_report, move_to_dups, normalize_root,
                        replace_with_hardlink)

HTML_FILE = APP_ROOT / "index.html"
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS: set[str] = set()     # filled in main()


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | error
        self.phase = ""
        self.done = 0
        self.total = 0
        self.error = None
        self.root = None
        self.result = None
        self.cancel = False

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "phase": self.phase, "done": self.done,
                    "total": self.total, "error": self.error, "root": self.root,
                    "has_result": self.result is not None}


JOB = Job()


def run_scan(root: Path, min_size: int) -> None:
    def progress(phase, done, total):
        with JOB.lock:
            JOB.phase, JOB.done, JOB.total = phase, done, total
    try:
        result = find_duplicates(root, min_size, exclude=[DUPS_DIR], progress=progress,
                                 cancel=lambda: JOB.cancel)
        with JOB.lock:
            JOB.result, JOB.state = result, "done"
    except Cancelled:
        with JOB.lock:
            JOB.state, JOB.phase = "idle", "Cancelled"
    except Exception as e:  # report anything unexpected to the page
        with JOB.lock:
            JOB.state, JOB.error = "error", f"{type(e).__name__}: {e}"


def _still_present(f: dict) -> bool:
    try:
        return os.path.getsize(f["path"]) == f["size"]
    except OSError:
        return False


def apply_actions(paths: list, mode: str) -> dict:
    with JOB.lock:
        result = JOB.result
        if JOB.state == "running" or not result:
            raise ValueError("No scan results - run a scan first.")
        index = {f["path"]: g for g in result["groups"] for f in g["files"]}
        targets = list(dict.fromkeys(p for p in paths if isinstance(p, str)))
        if not targets:
            raise ValueError("Nothing selected.")
        unknown = [p for p in targets if p not in index]
        if unknown:
            raise ValueError(f"Refusing: {len(unknown)} path(s) were not part of the scan results.")

        selected = set(targets)
        link_to: dict[int, str] = {}             # group id -> path of the copy to link to
        for g in {index[p]["id"]: index[p] for p in targets}.values():
            keepers = [f for f in g["files"] if f["path"] not in selected]
            if not keepers:
                raise ValueError(f"Refusing: set #{g['id']} would lose every copy. Keep at least one.")
            present = [f for f in keepers if _still_present(f)]
            if not present:
                raise ValueError(f"Set #{g['id']}: the copies marked 'keep' are gone or changed "
                                 "on disk. Rescan first.")
            link_to[g["id"]] = present[0]["path"]

        root = Path(result["root"])
        results, done_paths, freed = [], set(), 0
        for p in targets:
            g = index[p]
            entry = {"path": p, "ok": False}
            try:
                if os.path.getsize(p) != g["size"]:
                    entry["error"] = "file changed since the scan - skipped"
                elif mode == "move":
                    entry["dest"] = str(move_to_dups(p, root))
                    entry["ok"] = True
                elif mode == "link":
                    replace_with_hardlink(p, link_to[g["id"]])
                    entry["dest"] = link_to[g["id"]]
                    entry["ok"] = True
                else:
                    delete_file(p)
                    entry["ok"] = True
            except FileNotFoundError:
                entry["error"] = "file no longer exists"
                done_paths.add(p)               # gone either way; drop it from the list
            except OSError as e:
                entry["error"] = e.strerror or str(e)
            if entry["ok"]:
                done_paths.add(p)
                freed += g["size"]
            results.append(entry)

        remaining = []
        for g in result["groups"]:
            g["files"] = [f for f in g["files"] if f["path"] not in done_paths]
            if len(g["files"]) > 1:
                remaining.append(g)
        result["groups"] = remaining
        return {"results": results, "freed": freed, "groups": remaining, "dups_dir": str(DUPS_DIR)}


class Handler(BaseHTTPRequestHandler):
    server_version = "DedupWeb/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj))

    def _host_ok(self) -> bool:
        return not ALLOWED_HOSTS or (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 50 * 1024 * 1024:
            raise ValueError("Request too large.")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Bad Host header"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            config = json.dumps({"token": TOKEN, "dupsDir": str(DUPS_DIR), "sep": os.sep})
            html = HTML_FILE.read_text(encoding="utf-8").replace(
                "__CONFIG__", config.replace("</", "<\\/"))
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._token_ok():
            return self._json(403, {"error": "Missing or bad token - reload the page."})
        if path == "/api/status":
            return self._json(200, JOB.status())
        if path == "/api/result":
            with JOB.lock:
                return self._json(200, JOB.result or {})
        if path == "/api/report.txt":
            with JOB.lock:
                if not JOB.result:
                    return self._json(404, {"error": "No scan results"})
                text = format_report(JOB.result)
            return self._send(200, text, "text/plain; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="duplicates.txt"'})
        self._json(404, {"error": "Not found"})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "Bad Host header"})
        if not self._token_ok():
            return self._json(403, {"error": "Missing or bad token - reload the page."})
        path = urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": f"Bad request: {e}"})

        if path == "/api/scan":
            try:
                root = normalize_root(body.get("path", ""))
                min_size = max(0, int(body.get("min_size", 1)))
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            with JOB.lock:
                if JOB.state == "running":
                    return self._json(409, {"error": "A scan is already running."})
                JOB.state, JOB.phase, JOB.done, JOB.total = "running", "Starting", 0, 0
                JOB.error, JOB.result, JOB.cancel, JOB.root = None, None, False, str(root)
            threading.Thread(target=run_scan, args=(root, min_size), daemon=True).start()
            return self._json(200, {"ok": True, "root": str(root)})

        if path == "/api/cancel":
            with JOB.lock:
                JOB.cancel = True
            return self._json(200, {"ok": True})

        if path == "/api/apply":
            mode = body.get("mode") if body.get("mode") in ("move", "link") else "delete"
            try:
                return self._json(200, apply_actions(body.get("paths") or [], mode))
            except ValueError as e:
                return self._json(400, {"error": str(e)})

        self._json(404, {"error": "Not found"})


def main() -> int:
    ap = argparse.ArgumentParser(description="Web interface for finding and removing duplicate files.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1 - keep it local)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    if not HTML_FILE.exists():
        print(f"error: {HTML_FILE.name} must sit next to webapp.py", file=sys.stderr)
        return 2

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"error: no free port between {args.port} and {args.port + 19}", file=sys.stderr)
        return 2
    port = server.server_address[1]

    if args.host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    else:
        print("WARNING: listening on a non-local address. Anyone who can reach this machine\n"
              "and obtains the page can delete files the server's user can delete.")

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{port}/"
    print(f"Duplicate Finder running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
