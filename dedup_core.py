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

import errno
import filecmp
import fnmatch
import hashlib
import os
import re
import shutil
import stat
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Union

from dedup_config import APP_ROOT, KEEP_STRATEGIES, to_local_path  # noqa: F401 (re-exported)

PARTIAL_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024

ProgressFn = Optional[Callable[[str, int, int], None]]
CancelFn = Optional[Callable[[], bool]]
Roots = Union[Path, str, Sequence[Union[Path, str]]]

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FILE_ATTRIBUTE_HIDDEN = 0x2


class Cancelled(Exception):
    """Raised when a scan is cancelled through the cancel callback."""


# --------------------------------------------------------------------------- paths

def normalize_root(raw: str) -> Path:
    """Turn user input (Windows or Linux style) into an absolute, existing folder."""
    p = to_local_path(raw)
    if not p.exists():
        raise ValueError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"Not a folder: {p}")
    return p


def _norm(p: Union[str, Path]) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _is_within(path: Union[str, Path], folder: Union[str, Path]) -> bool:
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def collapse_roots(roots: Roots) -> list[Path]:
    """Unique folders, dropping any that sit inside another one in the list."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    unique = list(dict.fromkeys(Path(r) for r in roots))
    unique.sort(key=lambda p: len(str(p)))
    out: list[Path] = []
    for r in unique:
        if not any(_is_within(r, kept) for kept in out):
            out.append(r)
    return out


def root_for(path: str, roots: Sequence[Union[str, Path]]) -> Path:
    """The scanned folder that contains `path` (deepest match)."""
    matches = [Path(r) for r in roots if _is_within(path, r)]
    return max(matches, key=lambda r: len(str(r))) if matches else Path(path).parent


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- scanning

def scan_options(cfg) -> dict:
    """Scan keyword arguments taken from a Config."""
    return {
        "min_size": cfg["min_size"],
        "exclude_paths": [cfg.dups_dir],
        "exclude_dirs": cfg["exclude_dirs"],
        "exclude_files": cfg["exclude_files"],
        "skip_hidden": cfg["skip_hidden"],
    }


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


def _is_hidden(name: str, st: Optional[os.stat_result]) -> bool:
    if name.startswith("."):
        return True
    return bool(st is not None and getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_HIDDEN)


def _matcher(patterns: Iterable[str]):
    """Return (match_name, match_path) for a list of exclusion patterns.

    A pattern with no slash matches a file/folder *name* anywhere (wildcards allowed,
    e.g. ".Trash-*"); a pattern with a slash or drive is a full path to skip.
    """
    names, paths = [], []
    for pat in patterns:
        if re.search(r"[\\/]", pat) or re.match(r"^[A-Za-z]:", pat):
            try:
                paths.append(_norm(to_local_path(pat)))
            except ValueError:
                pass
        else:
            names.append(os.path.normcase(pat))

    def match_name(name: str) -> bool:
        n = os.path.normcase(name)
        return any(fnmatch.fnmatchcase(n, pat) for pat in names)

    def match_path(full: str) -> bool:
        f = _norm(full)
        return any(f == p or f.startswith(p.rstrip(os.sep) + os.sep) for p in paths)

    return match_name, match_path


def _collect_files(roots: list[Path], min_size: int, exclude_paths: Iterable[Path],
                   exclude_dirs: Iterable[str], exclude_files: Iterable[str], skip_hidden: bool,
                   progress: ProgressFn, cancel: CancelFn, errors: list):
    excluded = {_norm(Path(e).resolve()) for e in exclude_paths}
    dir_name_skip, dir_path_skip = _matcher(exclude_dirs)
    file_name_skip, file_path_skip = _matcher(exclude_files)
    by_size: dict[int, list] = defaultdict(list)
    seen_inodes = set()
    count = 0

    def on_error(err: OSError):
        errors.append(f"{getattr(err, 'filename', '?')}: {err.strerror or err}")

    def keep_dir(dirpath: str, d: str) -> bool:
        full = os.path.join(dirpath, d)
        if _norm(full) in excluded or dir_name_skip(d) or dir_path_skip(full):
            return False
        if skip_hidden:
            try:
                if _is_hidden(d, os.lstat(full)):
                    return False
            except OSError:
                return False
        return True

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
            if cancel and cancel():
                raise Cancelled()
            dirnames[:] = [d for d in dirnames if keep_dir(dirpath, d)]
            for name in filenames:
                if file_name_skip(name):
                    continue
                full = os.path.join(dirpath, name)
                if file_path_skip(full):
                    continue
                try:
                    st = os.lstat(full)
                except OSError as e:
                    errors.append(f"{full}: {e.strerror}")
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < min_size:
                    continue  # skips symlinks, devices, and files below min_size
                if skip_hidden and _is_hidden(name, st):
                    continue
                if st.st_ino:  # hard links point at the same data: not a real duplicate
                    key = (st.st_dev, st.st_ino)
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
                by_size[st.st_size].append({"path": full, "size": st.st_size,
                                            "mtime": st.st_mtime})
                count += 1
                if progress and count % 250 == 0:
                    progress("Listing files", count, 0)
    if progress:
        progress("Listing files", count, 0)
    return by_size, count


def find_duplicates(roots: Roots, min_size: int = 1, exclude_paths: Iterable[Path] = (),
                    exclude_dirs: Iterable[str] = (), exclude_files: Iterable[str] = (),
                    skip_hidden: bool = False,
                    progress: ProgressFn = None, cancel: CancelFn = None) -> dict:
    """Scan one or more folders recursively and return duplicate groups.

    Duplicates are matched across all the folders together.
    Returns {"roots", "root", "scanned_at", "files_scanned", "errors", "groups": [
        {"id", "hash", "size", "files": [{"path", "size", "mtime"}, ...]}, ...]}
    Groups are sorted by reclaimable space, largest first.
    """
    root_list = collapse_roots(roots)
    errors: list[str] = []
    by_size, total_files = _collect_files(root_list, min_size, exclude_paths, exclude_dirs,
                                          exclude_files, skip_hidden, progress, cancel, errors)

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
        "roots": [str(r) for r in root_list],
        "root": "; ".join(str(r) for r in root_list),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "files_scanned": total_files,
        "errors": errors,
        "groups": groups,
    }


def summarize(result: dict) -> dict:
    groups = result["groups"]
    files = sum(len(g["files"]) for g in groups)
    return {
        "files_scanned": result["files_scanned"],
        "sets": len(groups),
        "duplicate_files": files - len(groups),
        "reclaimable": sum(g["size"] * (len(g["files"]) - 1) for g in groups),
        "errors": len(result.get("errors", [])),
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

def file_meta(path: str) -> dict:
    """What history needs to undo an action later: size, modified time, permissions."""
    st = os.stat(path)
    return {"size": st.st_size, "mtime": st.st_mtime, "mode": stat.S_IMODE(st.st_mode)}


def dups_name(src: str, roots: Sequence[Union[str, Path]]) -> str:
    """'photo.jpg' from C:\\Pics\\2020\\trip -> 'photo [Pics_2020_trip].jpg'."""
    src_p = Path(src)
    folder = src_p.parent
    root = root_for(src, roots)
    try:
        parts = folder.relative_to(root.parent).parts
    except ValueError:
        parts = folder.parts
    clean = [_INVALID_NAME_CHARS.sub("", p).strip(" .") for p in parts]
    label = "_".join(p for p in clean if p) or "root"
    if len(label) > 100:                       # keep the deepest folders; stay under MAX_PATH
        label = label[-100:].lstrip("_")
    return f"{src_p.stem} [{label}]{src_p.suffix}"


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while True:
        candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def move_to_dups(src: str, roots: Sequence[Union[str, Path]], dups_dir: Path) -> Path:
    """Move a file into the dups folder, adding its source folder to the name."""
    dups_dir = Path(dups_dir)
    dups_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dups_dir / dups_name(src, roots))
    shutil.move(src, str(dest))
    return dest


def replace_with_hardlink(dup: str, keeper: str) -> None:
    """Replace `dup` with a hard link to `keeper`, so both paths share one copy of the data.

    The contents are compared byte for byte first. The link is created under a temporary
    name and then swapped in atomically, so if anything fails `dup` is left untouched.
    Both files must be on the same drive / filesystem, and that filesystem must support
    hard links (NTFS, ext4, APFS, ZFS ... - not FAT32/exFAT).
    """
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


# --------------------------------------------------------------------------- undo

def restore_move(original: str, dest: str) -> None:
    """Move a file from the dups folder back to where it came from."""
    if not os.path.exists(dest):
        raise FileNotFoundError(errno.ENOENT, "no longer in the dups folder", dest)
    if os.path.lexists(original):
        raise FileExistsError(errno.EEXIST, "original location is occupied", original)
    os.makedirs(os.path.dirname(original), exist_ok=True)
    shutil.move(dest, original)


def separate_link(path: str, mtime: Optional[float] = None, mode: Optional[int] = None) -> bool:
    """Turn a hard-linked path back into its own separate copy of the data.

    Restores the modified time and permissions the file had before it was linked.
    Returns False if the path was already a separate file (nothing to do).
    """
    st = os.stat(path)
    if st.st_nlink < 2:
        return False
    p = Path(path)
    tmp = p.with_name(f".{p.name}.dedup-{os.getpid()}.tmp")
    try:
        shutil.copyfile(path, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        try:
            os.replace(tmp, path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)   # read-only link on Windows
            os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


# --------------------------------------------------------------------------- report

def format_report(result: dict, status: Optional[dict] = None) -> str:
    """Plain-text list of all duplicate sets. `status` maps path -> (label, note)."""
    status = status or {}
    groups = result["groups"]
    s = summarize(result)
    roots = result.get("roots") or [result["root"]]
    lines = ["DUPLICATE FILES REPORT"]
    lines.append(f"Folder scanned : {roots[0]}")
    lines.extend(f"                 {r}" for r in roots[1:])
    lines += [
        f"Scanned at     : {result['scanned_at']}",
        f"Files scanned  : {result['files_scanned']:,}",
        f"Duplicate sets : {s['sets']:,}  ({s['sets'] + s['duplicate_files']:,} files, "
        f"{s['duplicate_files']:,} extra copies, {format_size(s['reclaimable'])} reclaimable)",
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
