#!/usr/bin/env python3
"""
dedup.py - find duplicate files and move the extra copies to the dups folder,
or replace them with hard links.

For every set of identical files one copy stays where it is; the others are moved
to the dups folder (set in config.json, default: dups/ next to this script) and
renamed to include the folder they came from:

    C:\\Photos\\backup\\img.jpg  ->  dups\\img [Photos_backup].jpg

Every run is saved in the history, so you can later recheck the same folders or
undo moves and links: see --history, --recheck and --restore.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from dedup_config import (CLI_ACTIONS, KEEP_STRATEGIES, ConfigError, load_config,
                          save_settings, write_default_config)
from dedup_core import (dups_name, file_meta, find_duplicates, format_report, format_size,
                        move_to_dups, normalize_root, pick_keeper, replace_with_hardlink,
                        scan_options, summarize, unique_path)
from dedup_history import open_history, parse_ids

EXAMPLES = r"""
scanning:
  python dedup.py "C:\Users\me\Pictures"          move duplicates (keeps oldest copy)
  python dedup.py D:\Photos E:\Backup             scan several folders together
  python dedup.py /home/me/Pictures --dry-run     only list what would happen
  python dedup.py D:\Music --link                 replace duplicates with hard links
  python dedup.py D:\Music --keep shortest        keep the copy with the shortest path

history:
  python dedup.py --history                       list past runs with their numbers
  python dedup.py --show 12                       every file run #12 touched
  python dedup.py --recheck 12 15                 scan the folders of runs #12 and #15 again
  python dedup.py --recheck all --dry-run         recheck every folder in history, change nothing
  python dedup.py --restore 12                    undo run #12 (moves back, links separated)
  python dedup.py --show 12 --json                one run as JSON (--history --json for all)
  python dedup.py --restore                       undo everything that can be undone
  python dedup.py --forget 3-7                    remove runs #3 to #7 from history

settings (config.json next to this script, or --config FILE):
  python dedup.py --set-dups-dir "D:\Duplicates"  permanently change the dups folder
  python dedup.py --show-config                   print the settings in effect
  python dedup.py --init-config                   create config.json with every default

Run numbers accept: all, 12, 3,5,9 and ranges like 3-7.
Deleted files (web page only) are listed in history but can't be restored.
"""


class _Progress:
    def __init__(self):
        self.last = 0.0

    def __call__(self, phase: str, done: int, total: int):
        now = time.monotonic()
        if now - self.last < 0.2 and (not total or done < total):
            return
        self.last = now
        text = f"{phase}: {done:,}/{total:,}" if total else f"{phase}: {done:,} found"
        sys.stderr.write("\r" + text.ljust(60))
        sys.stderr.flush()

    @staticmethod
    def clear():
        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()


# --------------------------------------------------------------------------- history output

def describe(run: dict) -> str:
    """One-line result of a run for the history list."""
    s, a = run["summary"], run["action"]
    if a == "scan":
        if not s.get("sets"):
            return f"no duplicates in {s.get('files_scanned', 0):,} files"
        return (f"{s['sets']:,} sets, {s['duplicate_files']:,} extra copies, "
                f"{format_size(s.get('reclaimable', 0))}")
    if a == "restore":
        parts = [f"{s.get('restored', 0)} moved back"] if s.get("restored") else []
        if s.get("separated"):
            parts.append(f"{s['separated']} unlinked")
        if s.get("failed"):
            parts.append(f"{s['failed']} failed")
        return ", ".join(parts) or "nothing to undo"
    verb = {"move": "moved", "link": "linked", "delete": "deleted"}.get(a, a)
    parts = [f"{run['n_done'] + run['n_undone']} {verb}"]
    if run["n_undone"]:
        parts.append(f"{run['n_undone']} undone")
    if run["n_restorable"]:
        parts.append(f"{run['n_restorable']} restorable")
    if run["n_failed"]:
        parts.append(f"{run['n_failed']} failed")
    return ", ".join(parts)


def print_history(hist, limit=None, as_json=False) -> None:
    if as_json:
        runs = hist.export()
        print(json.dumps(runs[-limit:] if limit else runs, indent=2))
        return
    runs = hist.list_runs(limit)
    if not runs:
        print("History is empty.")
        return
    rows = []
    for r in runs:
        roots = r["roots"]
        where = roots[0] if roots else ""
        if len(roots) > 1:
            where += f" (+{len(roots) - 1} more)"
        if r["note"]:
            where += f"  [{r['note']}]"
        rows.append((str(r["id"]), r["started"].replace("T", " ")[:16], r["source"],
                     r["action"], describe(r), where))
    head = ("#", "When", "From", "Action", "Result", "Folder(s)")
    w = [max(len(row[i]) for row in rows + [head]) for i in range(5)]
    for row in [head] + rows:
        print(f"{row[0]:>{w[0]}}  {row[1]:{w[1]}}  {row[2]:{w[2]}}  {row[3]:{w[3]}}  "
              f"{row[4]:{w[4]}}  {row[5]}")
    print(f"\nHistory file: {hist.path}")


def print_run(hist, run_id: int, as_json=False) -> int:
    run = hist.get_run(run_id)
    if not run:
        print(f"error: no history entry #{run_id}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(run, indent=2))
        return 0
    print(f"Run #{run['id']}  {run['started'].replace('T', ' ')}  ({run['source']}, {run['action']})"
          + (f"  {run['note']}" if run["note"] else ""))
    for r in run["roots"]:
        print(f"  folder: {r}")
    if run["settings"]:
        print("  settings: " + ", ".join(f"{k}={v}" for k, v in run["settings"].items()))
    if run["summary"]:
        print("  summary: " + ", ".join(f"{k}={v}" for k, v in run["summary"].items()))
    if run["items"]:
        width = max(len(i["status"]) for i in run["items"])
        print()
        for i in run["items"]:
            line = f"  [{i['status'].upper():{width}}] {i['path']}"
            if i["dest"] and i["action"] in ("move", "restore"):
                line += f"  <->  {i['dest']}"
            elif i["dest"]:
                line += f"  ->  linked to {i['dest']}"
            if i["error"]:
                line += f"  ({i['error']})"
            print(line)
    return 0


# --------------------------------------------------------------------------- scanning

def run_dedup(roots, cfg, hist, args, note: str = "") -> int:
    action = "link" if args.link else (args.action or cfg["cli_action"])
    keep = args.keep or cfg["keep"]
    opts = scan_options(cfg)
    if args.min_size is not None:
        opts["min_size"] = args.min_size

    print("Scanning " + "\n         ".join(str(r) for r in roots))
    progress = _Progress()
    try:
        result = find_duplicates(roots, **opts, progress=progress)
    except KeyboardInterrupt:
        progress.clear()
        print("Cancelled.")
        return 130
    progress.clear()

    groups = result["groups"]
    settings = {"keep": keep, "min_size": opts["min_size"]}
    summary = summarize(result)
    if not groups:
        print(f"No duplicates found among {result['files_scanned']:,} files.")
        run_id = hist.add_run("cli", "scan", result["roots"], settings, summary, note=note)
        print(f"Saved to history as #{run_id}.")
        return 0

    dups_dir = cfg.dups_dir
    status, items, done, failed, freed = {}, [], 0, 0, 0
    for g in groups:
        keep_i = pick_keeper(g["files"], keep)
        keeper = g["files"][keep_i]["path"]
        for i, f in enumerate(g["files"]):
            path = f["path"]
            if i == keep_i:
                status[path] = ("KEEP", "")
                continue
            if args.dry_run:
                status[path] = ("DUP", "-> would become a hard link to the KEEP copy" if action == "link"
                                else f"-> would move to {dups_dir / dups_name(path, result['roots'])}")
                continue
            item = {"path": path, "action": action, "size": g["size"], "mtime": f["mtime"]}
            try:
                item.update(file_meta(path))
                if action == "link":
                    replace_with_hardlink(path, keeper)
                    item.update(status="linked", dest=keeper)
                    status[path] = ("LINKED", "-> hard link to the KEEP copy")
                else:
                    dest = move_to_dups(path, result["roots"], dups_dir)
                    item.update(status="moved", dest=str(dest))
                    status[path] = ("MOVED", f"-> {dest}")
                done += 1
                freed += g["size"]
            except OSError as e:
                why = e.strerror or str(e)
                item.update(status="failed", error=why)
                status[path] = ("FAILED", f"({why})")
                failed += 1
            items.append(item)

    report = format_report(result, status)
    report_path = None
    if cfg["write_report"]:
        try:
            dups_dir.mkdir(parents=True, exist_ok=True)
            report_path = unique_path(dups_dir / f"report_{datetime.now():%Y%m%d_%H%M%S}.txt")
            report_path.write_text(report, encoding="utf-8")
        except OSError as e:
            print(f"warning: couldn't write the report to {dups_dir}: {e.strerror or e}",
                  file=sys.stderr)
            report_path = None

    if not args.quiet:
        print(report)
    if args.dry_run:
        print(f"Dry run: {summary['duplicate_files']:,} duplicate file(s) in {len(groups):,} "
              f"set(s); nothing changed.")
        run_id = hist.add_run("cli", "scan", result["roots"], settings, summary,
                              note=(note + "; " if note else "") + "dry run")
    else:
        settings["action"] = action
        summary.update({"done": done, "failed": failed, "freed": freed})
        run_id = hist.add_run("cli", action, result["roots"], settings, summary, items, note)
        if action == "link":
            print(f"Replaced {done:,} file(s) with hard links, freeing {format_size(freed)}"
                  + (f"; {failed} failed." if failed else "."))
        else:
            print(f"Moved {done:,} file(s) ({format_size(freed)}) to {dups_dir}"
                  + (f"; {failed} failed." if failed else "."))
        if done:
            print(f"Undo with:  python dedup.py --restore {run_id}")
    print(f"Saved to history as #{run_id}.")
    if report_path:
        print(f"Report: {report_path}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):        # don't crash on odd file names
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(
        description="Find duplicate files and move the extra copies to the dups folder "
                    "(or replace them with hard links).",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="folder(s) to scan (Windows or Linux paths)")
    ap.add_argument("--config", metavar="FILE", help="settings file (default: config.json next to the app)")

    g = ap.add_argument_group("scan options (defaults come from the config file)")
    g.add_argument("--action", choices=CLI_ACTIONS, help="what to do with duplicates")
    g.add_argument("--link", action="store_true", help="same as --action link")
    g.add_argument("--keep", choices=KEEP_STRATEGIES, help="which copy stays in place")
    g.add_argument("--min-size", type=int, metavar="BYTES", help="ignore files smaller than this")
    g.add_argument("--dry-run", action="store_true", help="list duplicates, change nothing")
    g.add_argument("--quiet", action="store_true", help="don't print the full report")

    h = ap.add_argument_group("history")
    h.add_argument("--history", action="store_true", help="list past runs")
    h.add_argument("--limit", type=int, metavar="N", help="with --history: show only the last N runs")
    h.add_argument("--show", type=int, metavar="ID", help="show every file a run touched")
    h.add_argument("--json", action="store_true", help="with --history or --show: print JSON")
    h.add_argument("--recheck", nargs="+", metavar="ID", help="scan the folders of these runs again")
    h.add_argument("--restore", nargs="*", metavar="ID",
                   help="undo these runs (no IDs = everything that can be undone)")
    h.add_argument("--forget", nargs="+", metavar="ID", help="remove runs from history")

    c = ap.add_argument_group("settings")
    c.add_argument("--set-dups-dir", metavar="PATH", help="permanently set the dups folder")
    c.add_argument("--show-config", action="store_true", help="print the settings in effect")
    c.add_argument("--init-config", action="store_true", help="create a config file with every default")
    args = ap.parse_args(argv)

    try:
        if args.init_config:
            print(f"Created {write_default_config(args.config)}")
            return 0
        if args.set_dups_dir:
            cfg = save_settings({"dups_dir": args.set_dups_dir}, args.config)
            print(f"Dups folder is now {cfg.dups_dir}  (saved in {cfg.path})")
            return 0
        cfg = load_config(args.config)
    except (ConfigError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for w in cfg.warnings:
        print(f"warning: {w}", file=sys.stderr)

    if args.show_config:
        print(f"Config file : {cfg.path}" + ("" if cfg.exists else "  (not created - all defaults)"))
        print(f"Dups folder : {cfg.dups_dir}")
        print(f"History file: {cfg.history_file}")
        print(json.dumps(cfg.as_dict(), indent=2))
        return 0

    try:
        hist = open_history(cfg)
    except Exception as e:  # sqlite errors, unwritable folder, ...
        print(f"error: can't open history file {cfg.history_file}: {e}", file=sys.stderr)
        return 2

    try:
        if args.history:
            print_history(hist, args.limit, args.json)
            return 0
        if args.show is not None:
            return print_run(hist, args.show, args.json)
        if args.forget:
            ids = parse_ids(args.forget, hist.existing_ids())
            print(f"Removed {hist.forget(ids)} run(s) from history.")
            return 0
        if args.restore is not None:
            ids = parse_ids(args.restore or ["all"], hist.existing_ids())
            res = hist.restore(ids, "cli")
            if not res["run_id"]:
                print("Nothing to undo in the selected run(s)."
                      + (f" {res['skipped_deleted']} file(s) were permanently deleted and can't be restored."
                         if res["skipped_deleted"] else ""))
                return 0
            print(f"Moved back {res['restored']} file(s); turned {res['separated']} hard link(s) "
                  "back into separate files.")
            if res["skipped_deleted"]:
                print(f"{res['skipped_deleted']} file(s) were permanently deleted and can't be restored.")
            for p in res["problems"]:
                print("  not restored: " + p)
            if res["run_id"]:
                print(f"Saved to history as #{res['run_id']}.")
            return 1 if res["problems"] else 0
        if args.recheck:
            ids = parse_ids(args.recheck, hist.existing_ids())
            roots, missing = [], []
            for r in hist.roots_for(ids):
                try:
                    roots.append(normalize_root(r))
                except ValueError:
                    missing.append(r)
            for m in missing:
                print(f"warning: folder no longer exists, skipped: {m}", file=sys.stderr)
            if not roots:
                print("error: none of the selected runs' folders exist any more.", file=sys.stderr)
                return 2
            note = "recheck of " + ", ".join(f"#{i}" for i in ids)
            return run_dedup(roots, cfg, hist, args, note)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not args.paths:
        ap.error("give a folder to scan, or use --history / --recheck / --restore (see --help)")
    try:
        roots = [normalize_root(p) for p in args.paths]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return run_dedup(roots, cfg, hist, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:            # output piped into `head`, `more`, ... that closed early
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
