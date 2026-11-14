#!/usr/bin/env python3
"""
webapp.py - local web interface for reviewing duplicate files.

    python webapp.py              # opens http://127.0.0.1:8765 in your browser
    python webapp.py --port 9000 --no-browser --config D:\\dedup\\config.json

Scan one or more folders, see every duplicate set and where each copy lives, choose
which copies to keep, then delete the rest, move them to the dups folder, or replace
them with hard links to a kept copy. Nothing is touched until you confirm.

The History tab lists every run (from here or the command line); select any or all
of them to scan their folders again or undo them. The Settings tab edits config.json.
Uses only the Python standard library.

Safety:
  * listens on 127.0.0.1 only, checks the Host header, and requires a per-run
    token on every API call, so other websites can't drive it;
  * the server only acts on paths that came from the current scan, refuses any
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

from dedup_config import (APP_ROOT, DEFAULTS, WEB_EDITABLE, ConfigError, load_config,
                          save_settings)
from dedup_core import (Cancelled, delete_file, file_meta, find_duplicates, format_report,
                        move_to_dups, normalize_root, replace_with_hardlink, scan_options,
                        summarize)
from dedup_history import open_history, parse_ids

HTML_FILE = APP_ROOT / "index.html"
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS: set[str] = set()     # filled in main()


class State:
    """Settings and history shared by all request threads."""
    def __init__(self):
        self.lock = threading.Lock()
        self.config_arg = None
        self.cfg = None
        self.hist = None

    def reload(self):
        cfg = load_config(self.config_arg)
        with self.lock:
            self.cfg = cfg
            self.hist = open_history(cfg)


APP = State()


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | error
        self.phase = ""
        self.done = 0
        self.total = 0
        self.error = None
        self.roots = []
        self.result = None
        self.cancel = False
        self.history_id = None

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "phase": self.phase, "done": self.done,
                    "total": self.total, "error": self.error, "roots": self.roots,
                    "has_result": self.result is not None, "history_id": self.history_id}


JOB = Job()


def start_scan(roots: list, min_size: int, note: str = "") -> None:
    with JOB.lock:
        if JOB.state == "running":
            raise RuntimeError("A scan is already running.")
        JOB.state, JOB.phase, JOB.done, JOB.total = "running", "Starting", 0, 0
        JOB.error, JOB.result, JOB.cancel = None, None, False
        JOB.roots, JOB.history_id = [str(r) for r in roots], None
    threading.Thread(target=run_scan, args=(roots, min_size, note), daemon=True).start()


def run_scan(roots: list, min_size: int, note: str) -> None:
    def progress(phase, done, total):
        with JOB.lock:
            JOB.phase, JOB.done, JOB.total = phase, done, total
    cfg, hist = APP.cfg, APP.hist
    opts = scan_options(cfg)
    opts["min_size"] = min_size
    try:
        result = find_duplicates(roots, **opts, progress=progress, cancel=lambda: JOB.cancel)
        run_id = hist.add_run("web", "scan", result["roots"], {"min_size": min_size},
                              summarize(result), note=note)
        with JOB.lock:
            JOB.result, JOB.state, JOB.history_id = result, "done", run_id
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
    cfg, hist = APP.cfg, APP.hist
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

        roots, dups_dir = result["roots"], cfg.dups_dir
        results, items, done_paths, freed = [], [], set(), 0
        for p in targets:
            g = index[p]
            entry = {"path": p, "ok": False}
            item = {"path": p, "action": mode, "size": g["size"]}
            try:
                if os.path.getsize(p) != g["size"]:
                    entry["error"] = "file changed since the scan - skipped"
                else:
                    item.update(file_meta(p))
                    if mode == "move":
                        entry["dest"] = str(move_to_dups(p, roots, dups_dir))
                        item.update(status="moved", dest=entry["dest"])
                    elif mode == "link":
                        replace_with_hardlink(p, link_to[g["id"]])
                        entry["dest"] = link_to[g["id"]]
                        item.update(status="linked", dest=entry["dest"])
                    else:
                        delete_file(p)
                        item.update(status="deleted")
                    entry["ok"] = True
            except FileNotFoundError:
                entry["error"] = "file no longer exists"
                done_paths.add(p)               # gone either way; drop it from the list
            except OSError as e:
                entry["error"] = e.strerror or str(e)
            if entry["ok"]:
                done_paths.add(p)
                freed += g["size"]
            else:
                item.update(status="failed", error=entry["error"])
            items.append(item)
            results.append(entry)

        ok = sum(1 for r in results if r["ok"])
        note = f"from scan #{JOB.history_id}" if JOB.history_id else ""
        run_id = hist.add_run("web", mode, roots, {"action": mode},
                              {"done": ok, "failed": len(results) - ok, "freed": freed},
                              items, note)

        remaining = []
        for g in result["groups"]:
            g["files"] = [f for f in g["files"] if f["path"] not in done_paths]
            if len(g["files"]) > 1:
                remaining.append(g)
        result["groups"] = remaining
        return {"results": results, "freed": freed, "groups": remaining,
                "dups_dir": str(dups_dir), "history_id": run_id}


def config_payload() -> dict:
    cfg = APP.cfg
    return {"values": cfg.as_dict(), "defaults": DEFAULTS, "editable": list(WEB_EDITABLE),
            "path": str(cfg.path), "exists": cfg.exists, "dups_dir": str(cfg.dups_dir),
            "history_file": str(cfg.history_file), "warnings": cfg.warnings}


class Handler(BaseHTTPRequestHandler):
    server_version = "DedupWeb/2.0"

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
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _ids(self, body: dict) -> list[int]:
        raw = body.get("ids")
        if raw == "all":
            raw = ["all"]
        if not isinstance(raw, list) or not raw:
            raise ValueError("Select at least one history entry.")
        return parse_ids([str(x) for x in raw], APP.hist.existing_ids())

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Bad Host header"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            boot = json.dumps({"token": TOKEN, "sep": os.sep})
            html = HTML_FILE.read_text(encoding="utf-8").replace(
                "__CONFIG__", boot.replace("</", "<\\/"))
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._token_ok():
            return self._json(403, {"error": "Missing or bad token - reload the page."})
        if path == "/api/status":
            return self._json(200, JOB.status())
        if path == "/api/result":
            with JOB.lock:
                return self._json(200, JOB.result or {})
        if path == "/api/config":
            return self._json(200, config_payload())
        if path == "/api/history":
            return self._json(200, {"runs": APP.hist.list_runs(), "path": str(APP.hist.path)})
        if path.startswith("/api/history/"):
            try:
                run = APP.hist.get_run(int(path.rsplit("/", 1)[1]))
            except ValueError:
                run = None
            return self._json(200, run) if run else self._json(404, {"error": "No such history entry"})
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

        try:
            if path == "/api/scan":
                raw = body.get("paths") or [body.get("path", "")]
                if isinstance(raw, str):
                    raw = [raw]
                roots = [normalize_root(p) for p in raw if str(p).strip()]
                if not roots:
                    raise ValueError("No path given.")
                min_size = body.get("min_size")
                min_size = APP.cfg["min_size"] if min_size is None else max(0, int(min_size))
                start_scan(roots, min_size)
                return self._json(200, {"ok": True, "roots": [str(r) for r in roots]})

            if path == "/api/cancel":
                with JOB.lock:
                    JOB.cancel = True
                return self._json(200, {"ok": True})

            if path == "/api/apply":
                mode = body.get("mode") if body.get("mode") in ("move", "link") else "delete"
                return self._json(200, apply_actions(body.get("paths") or [], mode))

            if path == "/api/config":
                updates = body.get("values") or {}
                if not isinstance(updates, dict):
                    raise ValueError("Expected settings to save.")
                bad = [k for k in updates if k not in WEB_EDITABLE]
                if bad:
                    raise ValueError(f"These settings can only be changed in the config file: {', '.join(bad)}")
                save_settings(updates, APP.config_arg)
                APP.reload()
                return self._json(200, config_payload())

            if path == "/api/history/recheck":
                ids = self._ids(body)
                roots, missing = [], []
                for r in APP.hist.roots_for(ids):
                    try:
                        roots.append(normalize_root(r))
                    except ValueError:
                        missing.append(r)
                if not roots:
                    raise ValueError("None of the selected entries' folders exist any more.")
                min_size = body.get("min_size")
                min_size = APP.cfg["min_size"] if min_size is None else max(0, int(min_size))
                start_scan(roots, min_size, "recheck of " + ", ".join(f"#{i}" for i in ids))
                return self._json(200, {"ok": True, "roots": [str(r) for r in roots],
                                        "missing": missing})

            if path == "/api/history/restore":
                with JOB.lock:
                    if JOB.state == "running":
                        raise RuntimeError("Wait for the scan to finish first.")
                return self._json(200, APP.hist.restore(self._ids(body), "web"))

            if path == "/api/history/forget":
                return self._json(200, {"removed": APP.hist.forget(self._ids(body))})
        except RuntimeError as e:
            return self._json(409, {"error": str(e)})
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": str(e)})

        self._json(404, {"error": "Not found"})


def main() -> int:
    ap = argparse.ArgumentParser(description="Web interface for finding and removing duplicate files.")
    ap.add_argument("--config", metavar="FILE", help="settings file (default: config.json next to the app)")
    ap.add_argument("--host", help="address to listen on (default from config: 127.0.0.1)")
    ap.add_argument("--port", type=int, help="port (default from config: 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    if not HTML_FILE.exists():
        print(f"error: {HTML_FILE.name} must sit next to webapp.py", file=sys.stderr)
        return 2
    APP.config_arg = args.config
    try:
        APP.reload()
    except (ConfigError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: can't open history file: {e}", file=sys.stderr)
        return 2
    for w in APP.cfg.warnings:
        print(f"warning: {w}", file=sys.stderr)

    host = args.host or APP.cfg["web_host"]
    first_port = args.port or APP.cfg["web_port"]
    server = None
    for port in range(first_port, first_port + 20):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"error: no free port between {first_port} and {first_port + 19}", file=sys.stderr)
        return 2
    port = server.server_address[1]

    if host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    else:
        print("WARNING: listening on a non-local address. Anyone who can reach this machine\n"
              "and obtains the page can delete files the server's user can delete.")

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"Duplicate Finder running at {url}  (Ctrl+C to stop)")
    print(f"Settings: {APP.cfg.path}{'' if APP.cfg.exists else ' (defaults)'}")
    if not args.no_browser and APP.cfg["open_browser"]:
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
