"""
dedup_core.py - shared duplicate-detection logic for dedup.py (CLI) and webapp.py (web UI).

Duplicates are found in three passes so large folder trees stay fast:
  1. group files by size (free - it comes from the directory listing)
  2. inside same-size groups, hash only the first 64 KB
  3. inside groups that still match, hash the whole file (SHA-256)
Only files that match on all three passes are reported as duplicates.

Works on Windows and Linux/macOS. Paths like C:\\Users\\me are accepted on
Windows; on Linux under WSL they are translated to /mnt/c/Users/me.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import os
import re
import shutil
import stat
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

APP_ROOT = Path(__file__).resolve().parent
DUPS_DIR = APP_ROOT / "dups"
MANIFEST = DUPS_DIR / "manifest.csv"

PARTIAL_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024
KEEP_STRATEGIES = ("oldest", "newest", "shortest")

ProgressFn = Optional[Callable[[str, int, int], None]]
CancelFn = Optional[Callable[[], bool]]

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class Cancelled(Exception):
    """Raised when a scan is cancelled through the cancel callback."""


# --------------------------------------------------------------------------- paths

def normalize_root(raw: str) -> Path:
    """Turn user input (Windows or Linux style) into an absolute, existing folder."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        raise ValueError("No path given.")

    win_drive = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", s)
    if os.name == "nt":
        if re.fullmatch(r"[A-Za-z]:", s):          # "D:" -> "D:\"
            s += "\\"
    elif win_drive:
        drive, rest = win_drive.group(1).lower(), win_drive.group(2) or ""
        wsl_mount = Path(f"/mnt/{drive}")
        if not wsl_mount.is_dir():
            raise ValueError(
                f"'{raw}' is a Windows path, but this is running on Linux/macOS. "
                "Use the folder's Linux path instead."
            )
        s = str(wsl_mount / rest.replace("\\", "/"))
    elif s.startswith("\\\\"):
        raise ValueError("Network paths like \\\\server\\share only work on Windows; "
                         "mount the share and use its Linux path.")

    p = Path(os.path.expandvars(os.path.expanduser(s))).resolve()
    if not p.exists():
        raise ValueError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"Not a folder: {p}")
    return p


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- scanning

def _hash_file(path: str, limit: Optional[int] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if limit is not None:
            h.update(f.read(limit))
        else:
            while True:
                chunk = f.read(CHUNK_BYTES)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def _collect_files(root: Path, min_size: int, exclude: Iterable[Path],
                   progress: ProgressFn, cancel: CancelFn, errors: list):
    excluded = {os.path.normcase(str(Path(e).resolve())) for e in exclude}
    by_size: dict[int, list] = defaultdict(list)
    seen_inodes = set()
    count = 0

    def on_error(err: OSError):
        errors.append(f"{getattr(err, 'filename', '?')}: {err.strerror or err}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        if cancel and cancel():
            raise Cancelled()
        dirnames[:] = [d for d in dirnames
                       if os.path.normcase(os.path.join(dirpath, d)) not in excluded]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError as e:
                errors.append(f"{full}: {e.strerror}")
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size < min_size:
                continue  # skips symlinks, devices, and files below min_size
            if st.st_ino:  # hard links point at the same data: not a real duplicate
                key = (st.st_dev, st.st_ino)
                if key in seen_inodes:
                    continue
                seen_inodes.add(key)
            by_size[st.st_size].append({"path": full, "size": st.st_size, "mtime": st.st_mtime})
            count += 1
            if progress and count % 250 == 0:
                progress("Listing files", count, 0)
    if progress:
        progress("Listing files", count, 0)
    return by_size, count


def find_duplicates(root: Path, min_size: int = 1, exclude: Iterable[Path] = (),
                    progress: ProgressFn = None, cancel: CancelFn = None) -> dict:
    """Scan `root` recursively and return duplicate groups.

    Returns {"root", "scanned_at", "files_scanned", "errors", "groups": [
        {"id", "hash", "size", "files": [{"path", "size", "mtime"}, ...]}, ...]}
    Groups are sorted by reclaimable space, largest first.
    """
    root = Path(root)
    errors: list[str] = []
    by_size, total_files = _collect_files(root, min_size, exclude, progress, cancel, errors)

    candidates = [g for g in by_size.values() if len(g) > 1]
    todo = sum(len(g) for g in candidates)
    done = 0

    def tick():
        nonlocal done
        done += 1
        if cancel and cancel():
            raise Cancelled()
        if progress and (done % 25 == 0 or done >= todo):
            progress("Comparing contents", done, todo)

    def group_by_hash(files, limit):
        out = defaultdict(list)
        for f in files:
            try:
                out[_hash_file(f["path"], limit)].append(f)
            except OSError as e:
                errors.append(f"{f['path']}: {e.strerror or e}")
            tick()
        return out

    groups = []
    for same_size in candidates:
        size = same_size[0]["size"]
        for p_hash, bucket in group_by_hash(same_size, PARTIAL_BYTES).items():
            if len(bucket) < 2:
                continue
            if size <= PARTIAL_BYTES:          # partial hash already covered the whole file
                full = {p_hash: bucket}
            else:
                todo += len(bucket)
                full = group_by_hash(bucket, None)
            for f_hash, files in full.items():
                if len(files) > 1:
                    files.sort(key=lambda f: f["path"].lower())
                    groups.append({"hash": f_hash, "size": size, "files": files})

    groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
    for i, g in enumerate(groups, 1):
        g["id"] = i
    return {
        "root": str(root),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "files_scanned": total_files,
        "errors": errors,
        "groups": groups,
    }


def pick_keeper(files: list, strategy: str = "oldest") -> int:
    """Index of the copy to keep: oldest/newest by modified time, or shortest path."""
    def key(i):
        f = files[i]
        if strategy == "newest":
            return (-f["mtime"], len(f["path"]), f["path"])
        if strategy == "shortest":
            return (len(f["path"]), f["mtime"], f["path"])
        return (f["mtime"], len(f["path"]), f["path"])
    return min(range(len(files)), key=key)


# --------------------------------------------------------------------------- actions

def dups_name(src: str, root: Path) -> str:
    """'photo.jpg' from C:\\Pics\\2020\\trip -> 'photo [Pics_2020_trip].jpg'."""
    src_p = Path(src)
    folder = src_p.parent
    try:
        parts = folder.relative_to(Path(root).parent).parts
    except ValueError:
        parts = folder.parts
    clean = [_INVALID_NAME_CHARS.sub("", p).strip(" .") for p in parts]
    label = "_".join(p for p in clean if p) or "root"
    if len(label) > 100:                       # keep the deepest folders; stay under MAX_PATH
        label = label[-100:].lstrip("_")
    return f"{src_p.stem} [{label}]{src_p.suffix}"


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while True:
        candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def move_to_dups(src: str, root: Path) -> Path:
    """Move a file into <app root>/dups and record it in dups/manifest.csv."""
    DUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(DUPS_DIR / dups_name(src, root))
    shutil.move(src, str(dest))
    new_file = not MANIFEST.exists()
    with open(MANIFEST, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["moved_at", "original_path", "dups_path"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), src, str(dest)])
    return dest


def replace_with_hardlink(dup: str, keeper: str) -> None:
    """Replace `dup` with a hard link to `keeper`, so both paths share one copy of the data.

    The contents are compared byte for byte first. The link is created under a temporary
    name and then swapped in atomically, so if anything fails `dup` is left untouched.
    Both files must be on the same drive / filesystem, and that filesystem must support
    hard links (NTFS, ext4, APFS, ... - not FAT32/exFAT).
    """
    import filecmp
    k_st, d_st = os.stat(keeper), os.stat(dup)
    if (k_st.st_dev, k_st.st_ino) == (d_st.st_dev, d_st.st_ino):
        return                                   # already the same file
    if k_st.st_dev != d_st.st_dev:
        raise OSError(errno.EXDEV, "on a different drive / filesystem than the kept copy")
    if k_st.st_size != d_st.st_size or not filecmp.cmp(keeper, dup, shallow=False):
        raise OSError(errno.EINVAL, "contents differ from the kept copy - skipped")

    d = Path(dup)
    tmp = d.with_name(f".{d.name}.dedup-{os.getpid()}.tmp")
    try:
        os.link(keeper, tmp)
    except OSError as e:
        unsupported = {getattr(errno, n, None) for n in ("EPERM", "EOPNOTSUPP", "ENOTSUP")}
        if e.errno in unsupported or os.name == "nt":
            e.strerror = f"hard link failed ({e.strerror}); filesystem may not support hard links"
        raise
    try:
        try:
            os.replace(tmp, dup)
        except PermissionError:
            os.chmod(dup, stat.S_IWRITE | stat.S_IREAD)   # read-only dup on Windows
            os.replace(tmp, dup)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def delete_file(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)            # clear read-only flag (Windows)
        os.remove(path)


def restore_from_manifest() -> tuple[int, list[str]]:
    """Move every file listed in dups/manifest.csv back to where it came from."""
    if not MANIFEST.exists():
        return 0, []
    with open(MANIFEST, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    restored, problems, keep_rows = 0, [], []
    for r in rows:
        src, dst = r["original_path"], r["dups_path"]
        if not os.path.exists(dst):
            continue                            # already restored or removed by hand
        if os.path.exists(src):
            problems.append(f"Not restored (original location is occupied): {src}")
            keep_rows.append(r)
            continue
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
            restored += 1
        except OSError as e:
            problems.append(f"Not restored ({e.strerror or e}): {src}")
            keep_rows.append(r)
    with open(MANIFEST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["moved_at", "original_path", "dups_path"])
        w.writeheader()
        w.writerows(keep_rows)
    return restored, problems


# --------------------------------------------------------------------------- report

def format_report(result: dict, status: Optional[dict] = None) -> str:
    """Plain-text list of all duplicate sets. `status` maps path -> (label, note)."""
    status = status or {}
    groups = result["groups"]
    n_files = sum(len(g["files"]) for g in groups)
    reclaim = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    lines = [
        "DUPLICATE FILES REPORT",
        f"Folder scanned : {result['root']}",
        f"Scanned at     : {result['scanned_at']}",
        f"Files scanned  : {result['files_scanned']:,}",
        f"Duplicate sets : {len(groups):,}  ({n_files:,} files, "
        f"{n_files - len(groups):,} extra copies, {format_size(reclaim)} reclaimable)",
        "",
    ]
    width = max((len(lbl) for lbl, _ in status.values()), default=0)
    for g in groups:
        lines.append(f"=== Set {g['id']}: {len(g['files'])} copies x {format_size(g['size'])}"
                     f"  (sha256 {g['hash'][:16]}) ===")
        for f in g["files"]:
            label, note = status.get(f["path"], ("", ""))
            when = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
            prefix = f"[{label}]".ljust(width + 2) + " " if width else ""
            lines.append(f"  {prefix}{f['path']}   (modified {when})" + (f"  {note}" if note else ""))
        lines.append("")
    if result.get("errors"):
        lines.append(f"--- {len(result['errors'])} item(s) could not be read ---")
        lines.extend(f"  {e}" for e in result["errors"])
    return "\n".join(lines)
