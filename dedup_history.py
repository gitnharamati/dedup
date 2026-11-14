"""
dedup_history.py - permanent record of every scan and action (CLI and web).

Each run is one entry: when, where it came from (cli/web), the folder(s), the action
(scan, move, link, delete, restore) and, for actions, every file it touched with the
result. Moved and linked files can be undone later; deleted files are listed but
can't be brought back.

Stored in SQLite (history.db by default) so the command line and the web page can use
it at the same time. `dedup.py --history --json` exports it as JSON.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from dedup_core import restore_move, separate_link

SCHEMA_VERSION = 1

# item status values
MOVED, LINKED, DELETED, FAILED = "moved", "linked", "deleted", "failed"
RESTORED, SEPARATED, MISSING = "restored", "separated", "missing"
RESTORABLE = {("move", MOVED), ("link", LINKED)}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    started  TEXT NOT NULL,
                    source   TEXT NOT NULL,
                    action   TEXT NOT NULL,
                    roots    TEXT NOT NULL,
                    settings TEXT NOT NULL DEFAULT '{}',
                    summary  TEXT NOT NULL DEFAULT '{}',
                    note     TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS items (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    path    TEXT NOT NULL,
                    action  TEXT NOT NULL,
                    dest    TEXT,
                    size    INTEGER,
                    mtime   REAL,
                    mode    INTEGER,
                    status  TEXT NOT NULL,
                    error   TEXT,
                    updated TEXT
                );
                CREATE INDEX IF NOT EXISTS items_run ON items(run_id);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                       (str(SCHEMA_VERSION),))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    # ------------------------------------------------------------------ writing
    def add_run(self, source: str, action: str, roots: Iterable[str], settings: dict = None,
                summary: dict = None, items: Iterable[dict] = (), note: str = "") -> int:
        """Record one run. items: {path, action, status, dest?, size?, mtime?, mode?, error?}"""
        with closing(self._connect()) as db, db:
            cur = db.execute(
                "INSERT INTO runs (started, source, action, roots, settings, summary, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), source, action, json.dumps(list(map(str, roots))),
                 json.dumps(settings or {}), json.dumps(summary or {}), note))
            run_id = cur.lastrowid
            db.executemany(
                "INSERT INTO items (run_id, path, action, dest, size, mtime, mode, status, error, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, it["path"], it["action"], it.get("dest"), it.get("size"),
                  it.get("mtime"), it.get("mode"), it["status"], it.get("error"), _now())
                 for it in items])
            return run_id

    def _set_status(self, db, item_id: int, status: str, error: Optional[str] = None) -> None:
        db.execute("UPDATE items SET status = ?, error = ?, updated = ? WHERE id = ?",
                   (status, error, _now(), item_id))

    def forget(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        with closing(self._connect()) as db, db:
            q = ",".join("?" * len(ids))
            return db.execute(f"DELETE FROM runs WHERE id IN ({q})", ids).rowcount

    # ------------------------------------------------------------------ reading
    def _run_dict(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("roots", "settings", "summary"):
            d[k] = json.loads(d[k] or ("[]" if k == "roots" else "{}"))
        return d

    def list_runs(self, limit: Optional[int] = None) -> list[dict]:
        sql = """
            SELECT r.*,
                   COUNT(i.id) AS n_items,
                   SUM(CASE WHEN (i.action='move' AND i.status='moved')
                              OR (i.action='link' AND i.status='linked') THEN 1 ELSE 0 END) AS n_restorable,
                   SUM(CASE WHEN i.status IN ('moved','linked','deleted') THEN 1 ELSE 0 END) AS n_done,
                   SUM(CASE WHEN i.status IN ('restored','separated') THEN 1 ELSE 0 END) AS n_undone,
                   SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) AS n_failed,
                   SUM(CASE WHEN i.status = 'deleted' THEN 1 ELSE 0 END) AS n_deleted
            FROM runs r LEFT JOIN items i ON i.run_id = r.id
            GROUP BY r.id ORDER BY r.id DESC"""
        if limit:
            sql += f" LIMIT {int(limit)}"
        with closing(self._connect()) as db:
            out = []
            for r in db.execute(sql):
                d = self._run_dict(r)
                for k in ("n_restorable", "n_done", "n_undone", "n_failed", "n_deleted"):
                    d[k] = d[k] or 0
                out.append(d)
            return out

    def get_run(self, run_id: int) -> Optional[dict]:
        with closing(self._connect()) as db:
            r = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not r:
                return None
            d = self._run_dict(r)
            d["items"] = [dict(i) for i in
                          db.execute("SELECT * FROM items WHERE run_id = ? ORDER BY id", (run_id,))]
            return d

    def existing_ids(self) -> list[int]:
        with closing(self._connect()) as db:
            return [r[0] for r in db.execute("SELECT id FROM runs ORDER BY id DESC")]

    def roots_for(self, ids: Iterable[int]) -> list[str]:
        roots: list[str] = []
        with closing(self._connect()) as db:
            for i in ids:
                r = db.execute("SELECT roots FROM runs WHERE id = ?", (i,)).fetchone()
                if r:
                    roots.extend(json.loads(r[0]))
        return list(dict.fromkeys(roots))

    def export(self) -> list[dict]:
        return [self.get_run(r["id"]) for r in reversed(self.list_runs())]

    # ------------------------------------------------------------------ undo
    def restore(self, ids: Iterable[int], source: str = "cli") -> dict:
        """Undo every restorable file in the given runs.

        Moved files go back to their original location; linked files become separate
        copies again (with their original modified time and permissions). Deleted files
        can't be restored and are skipped. The restore itself is recorded as a new run.
        """
        ids = list(ids)
        if not ids:
            return {"restored": 0, "separated": 0, "problems": [], "run_id": None, "skipped_deleted": 0}
        q = ",".join("?" * len(ids))
        with closing(self._connect()) as db:
            rows = [dict(r) for r in db.execute(
                f"SELECT * FROM items WHERE run_id IN ({q}) ORDER BY run_id, id", ids)]
        restored = separated = 0
        problems, log = [], []
        with closing(self._connect()) as db, db:
            for it in rows:
                if (it["action"], it["status"]) not in RESTORABLE:
                    continue
                entry = {"path": it["path"], "action": "restore", "size": it["size"]}
                try:
                    if it["action"] == "move":
                        restore_move(it["path"], it["dest"])
                        self._set_status(db, it["id"], RESTORED)
                        entry.update(status=RESTORED, dest=it["dest"])
                        restored += 1
                    else:
                        separate_link(it["path"], it["mtime"], it["mode"])
                        self._set_status(db, it["id"], SEPARATED)
                        entry.update(status=SEPARATED)
                        separated += 1
                except FileNotFoundError as e:
                    why = e.strerror if it["action"] == "move" else "file no longer exists"
                    self._set_status(db, it["id"], MISSING, why)
                    problems.append(f"{it['path']}: {why}")
                    entry.update(status=FAILED, error=why)
                except OSError as e:
                    why = e.strerror or str(e)
                    problems.append(f"{it['path']}: {why}")
                    entry.update(status=FAILED, error=why)
                log.append(entry)
        skipped_deleted = sum(1 for it in rows if it["status"] == DELETED)
        run_id = None
        if log:
            # rows were read before anything changed, so this is the runs that had work to undo
            undone = sorted({it["run_id"] for it in rows if (it["action"], it["status"]) in RESTORABLE})
            roots = self.roots_for(undone)
            note = "undo of " + ", ".join(f"#{i}" for i in undone)
            run_id = self.add_run(source, "restore", roots, summary={
                "restored": restored, "separated": separated, "failed": len(problems)},
                items=log, note=note)
        return {"restored": restored, "separated": separated, "problems": problems,
                "run_id": run_id, "skipped_deleted": skipped_deleted}

    # ------------------------------------------------------------------ migration
    def import_legacy_manifest(self, manifest: Path) -> int:
        """Bring moves logged by older versions (dups/manifest.csv) into history, once."""
        manifest = Path(manifest)
        if not manifest.is_file():
            return 0
        with open(manifest, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("original_path") and r.get("dups_path")]
        items = [{"path": r["original_path"], "action": "move", "dest": r["dups_path"],
                  "status": MOVED if os.path.exists(r["dups_path"]) else MISSING} for r in rows]
        if items:
            roots = sorted({os.path.dirname(i["path"]) for i in items})
            self.add_run("cli", "move", roots, summary={"moved": len(items)}, items=items,
                         note=f"imported from {manifest.name}")
        manifest.rename(manifest.with_name("manifest.imported.csv"))
        return len(items)


def open_history(cfg) -> History:
    """Open the history file named in the config, importing any old manifest.csv once."""
    hist = History(cfg.history_file)
    for manifest in {cfg.dups_dir / "manifest.csv", Path(__file__).resolve().parent / "dups" / "manifest.csv"}:
        try:
            hist.import_legacy_manifest(manifest)
        except (OSError, csv.Error, KeyError):
            pass
    return hist


def parse_ids(tokens: Iterable[str], available: Iterable[int]) -> list[int]:
    """'all', '3', '3,5', '2-6' (any mix) -> sorted list of existing run ids."""
    available = list(available)
    chosen: set[int] = set()
    for tok in tokens:
        for part in str(tok).replace(",", " ").split():
            part = part.lstrip("#")
            try:
                if part.lower() == "all":
                    chosen.update(available)
                elif "-" in part:
                    a, b = part.split("-", 1)
                    lo, hi = sorted((int(a), int(b)))
                    chosen.update(i for i in range(lo, hi + 1) if i in available)
                else:
                    chosen.add(int(part))
            except ValueError:
                raise ValueError(f'"{part}" is not a run number (use e.g. 12, 3,5, 2-6 or all)')
    missing = sorted(chosen - set(available))
    if missing:
        raise ValueError("No history entry " + ", ".join(f"#{m}" for m in missing))
    return sorted(chosen)
