#!/usr/bin/env python3
"""
dedup.py - find duplicate files under a folder and move the extra copies to
the 'dups' folder next to this script.

For every set of identical files one copy stays where it is; the others are moved
to <app root>/dups and renamed to include the folder they came from:

    C:\\Photos\\backup\\img.jpg  ->  dups\\img [Photos_backup].jpg

A full text report is written to dups/report_<timestamp>.txt, and every move is
logged in dups/manifest.csv so `--restore` can put everything back.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from dedup_core import (DUPS_DIR, KEEP_STRATEGIES, dups_name, find_duplicates,
                        format_report, format_size, move_to_dups, normalize_root,
                        pick_keeper, replace_with_hardlink, restore_from_manifest)

EXAMPLES = r"""
examples:
  python dedup.py "C:\Users\me\Pictures"            move duplicates (keeps oldest copy)
  python dedup.py /home/me/Pictures --dry-run       only list what would be moved
  python dedup.py D:\Music --keep shortest          keep the copy with the shortest path
  python dedup.py D:\Music --link                   replace duplicates with hard links
  python dedup.py --restore                         move everything in dups/ back

--link keeps every path in place, but all copies then share one set of data on disk:
editing any one of them changes them all. The kept and linked files must be on the
same drive, on a filesystem with hard links (NTFS, ext4, APFS...; not FAT32/exFAT).
Linked files take the kept copy's modified date and permissions. --restore only
undoes moves, not links.
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


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):        # don't crash on odd file names
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(
        description="Find duplicate files and move the extra copies to the 'dups' folder.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="folder to scan (Windows or Linux path)")
    ap.add_argument("--keep", choices=KEEP_STRATEGIES, default="oldest",
                    help="which copy stays in place (default: oldest)")
    ap.add_argument("--min-size", type=int, default=1, metavar="BYTES",
                    help="ignore files smaller than this (default 1 = skip empty files)")
    ap.add_argument("--link", action="store_true",
                    help="replace duplicates with hard links to the kept copy instead of moving them")
    ap.add_argument("--dry-run", action="store_true", help="list duplicates, change nothing")
    ap.add_argument("--quiet", action="store_true", help="don't print the full report")
    ap.add_argument("--restore", action="store_true",
                    help="move every file in dups/ back to its original location")
    args = ap.parse_args(argv)

    if args.restore:
        restored, problems = restore_from_manifest()
        print(f"Restored {restored} file(s).")
        for p in problems:
            print("  " + p)
        return 1 if problems else 0

    if not args.path:
        ap.error("a PATH is required (or use --restore)")
    try:
        root = normalize_root(args.path)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Scanning {root}")
    progress = _Progress()
    try:
        result = find_duplicates(root, args.min_size, exclude=[DUPS_DIR], progress=progress)
    except KeyboardInterrupt:
        progress.clear()
        print("Cancelled.")
        return 130
    progress.clear()

    groups = result["groups"]
    if not groups:
        print(f"No duplicates found among {result['files_scanned']:,} files.")
        return 0

    status, moved, failed, freed = {}, 0, 0, 0
    for g in groups:
        keep = pick_keeper(g["files"], args.keep)
        keeper = g["files"][keep]["path"]
        for i, f in enumerate(g["files"]):
            if i == keep:
                status[f["path"]] = ("KEEP", "")
            elif args.dry_run:
                note = (f"-> would become a hard link to the KEEP copy" if args.link
                        else f"-> would move to dups/{dups_name(f['path'], root)}")
                status[f["path"]] = ("DUP", note)
            else:
                try:
                    if args.link:
                        replace_with_hardlink(f["path"], keeper)
                        status[f["path"]] = ("LINKED", "-> hard link to the KEEP copy")
                    else:
                        dest = move_to_dups(f["path"], root)
                        status[f["path"]] = ("MOVED", f"-> dups/{dest.name}")
                    moved += 1
                    freed += g["size"]
                except OSError as e:
                    status[f["path"]] = ("FAILED", f"({e.strerror or e})")
                    failed += 1

    report = format_report(result, status)
    DUPS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = DUPS_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.txt"
    report_path.write_text(report, encoding="utf-8")

    if not args.quiet:
        print(report)
    extra = sum(len(g["files"]) - 1 for g in groups)
    if args.dry_run:
        print(f"Dry run: {extra:,} duplicate file(s) in {len(groups):,} set(s); nothing changed.")
    elif args.link:
        print(f"Replaced {moved:,} file(s) with hard links, freeing {format_size(freed)}"
              + (f"; {failed} failed." if failed else "."))
    else:
        print(f"Moved {moved:,} file(s) ({format_size(freed)}) to {DUPS_DIR}"
              + (f"; {failed} failed." if failed else "."))
        print("Undo with:  python dedup.py --restore")
    print(f"Report: {report_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
