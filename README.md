# Duplicate Finder

Finds files with identical content and moves, hard-links or deletes the extra copies.
Command line and local web page; Python 3.8+, standard library only, Windows or Linux.

| File | What it is |
|---|---|
| `dedup.py` | Command line: scan, move duplicates to the dups folder or hard-link them, history, undo |
| `webapp.py` + `index.html` | Local web page: review duplicates, choose what to keep, History and Settings tabs |
| `dedup_core.py` | Duplicate detection and file actions, shared by both |
| `dedup_config.py` | Reads and writes the settings file |
| `dedup_history.py` | Permanent history of runs, recheck and restore |
| `config.example.json` | Every setting with its default |

Created as you use it (not in git): `config.json` (your settings), `history.db` (your history)
and the dups folder.

## Command line

```
python dedup.py "C:\Users\me\Pictures"        # move duplicates, keep the oldest copy
python dedup.py D:\Photos E:\Backup           # scan several folders together
python dedup.py /home/me/Pictures --dry-run   # only list them
python dedup.py D:\Music --keep shortest      # keep: oldest | newest | shortest (path)
python dedup.py D:\Music --link               # replace duplicates with hard links
```

For each duplicate set, one copy stays put and the rest go to the dups folder,
with the source folder added to the name:

```
C:\Photos\backup\2020\img.jpg  ->  dups\img [Photos_backup_2020].jpg
```

Each run also writes `report_<date>.txt` in the dups folder (turn off with `"write_report": false`).

## Settings (`config.json`)

All behaviour can be set in a JSON file. The app looks for, in order: `--config FILE`,
the `DEDUP_CONFIG` environment variable, then `config.json` next to the app.
The file is optional, and any setting you leave out uses its default, so a file can be as small as:

```json
{ "dups_dir": "D:\\Duplicates", "keep": "newest" }
```

Create a file with every default to edit: `python dedup.py --init-config`
(or copy `config.example.json`). Check what's in effect: `python dedup.py --show-config`.
The web page's **Settings** tab edits the same file.

| Setting | Default | What it does |
|---|---|---|
| `dups_dir` | `"dups"` | Where moved duplicates and reports go. Relative paths are relative to the config file's folder. |
| `history_file` | `"history.db"` | Where history is kept. |
| `keep` | `"oldest"` | Which copy stays: `oldest`, `newest` or `shortest` (path). |
| `min_size` | `1` | Ignore files smaller than this many bytes (1 skips only empty files). |
| `cli_action` | `"move"` | What `dedup.py` does: `move` or `link`. |
| `web_action` | `"delete"` | Pre-selected action on the web page: `delete`, `move` or `link`. |
| `exclude_dirs` | `.git`, `.zfs`, `$RECYCLE.BIN`, ... | Folders to skip. A name (wildcards OK, e.g. `.Trash-*`) skips it anywhere; a full path skips that one folder. |
| `exclude_files` | `Thumbs.db`, `desktop.ini`, `.DS_Store` | Files to skip, same rules (e.g. `*.tmp`). |
| `skip_hidden` | `false` | Skip hidden files and folders (dot-names, and the Windows hidden attribute). |
| `write_report` | `true` | Command line: save a text report in the dups folder on each run. |
| `web_host` / `web_port` | `127.0.0.1` / `8765` | Where the web page listens. |
| `open_browser` | `true` | Open a browser tab when `webapp.py` starts. |

Command-line flags override the file for one run (`--keep`, `--min-size`, `--action`, `--link`).
Keys starting with `_` are ignored, so you can keep notes in the file (`"_comment": "..."`).
In JSON, Windows paths need doubled backslashes (`"D:\\Duplicates"`) or forward slashes (`"D:/Duplicates"`).

### Setting the dups folder permanently

```
python dedup.py --set-dups-dir "D:\Duplicates"
```

or **Settings → Dups folder** on the web page. Both save it to `config.json`; the folder is
created if needed and checked for write access. Changing it later is safe: history remembers
where every file was moved, so earlier moves can still be undone from the old folder.

## History: recheck and restore

Every scan and action, from the command line or the web page, is saved permanently with its
folder(s), action and, for actions, every file it touched.

```
python dedup.py --history                 # list runs with their numbers
python dedup.py --show 12                 # every file run #12 touched
python dedup.py --recheck 12 15           # scan the folders of runs #12 and #15 again (together)
python dedup.py --recheck all --dry-run   # recheck every folder in history, change nothing
python dedup.py --restore 12              # undo run #12
python dedup.py --restore                 # undo everything that can be undone
python dedup.py --forget 3-7              # remove runs #3 to #7 from history (files untouched)
python dedup.py --history --json          # export history as JSON
```

Run numbers accept `all`, single numbers, lists (`3,5,9`) and ranges (`3-7`).
On the web page, the **History** tab does the same: tick any entries (or all) and press
**Recheck for duplicates**, **Restore** or **Remove from history**. Click an entry to see its files.

What restore does:

- **Moved files** go back from the dups folder to their original location. If something
  now occupies that location, the file is left in the dups folder and reported.
- **Hard-linked files** become separate copies again, with the modified date and
  permissions they had before linking. This uses the disk space again.
- **Deleted files** (web page only) are listed but can't be restored.

A recheck uses the folders as they are now, with your current settings, and folders that no
longer exist are skipped. A recheck runs the command line's normal action, so add `--dry-run`
to only look. Restores are logged in history too.

If you used an older version, its `dups/manifest.csv` is imported into history automatically
the first time, so those moves can still be undone.

## Hard links (`--link`)

Instead of moving duplicates, `--link` replaces each one with a hard link to the kept
copy. Every path keeps working, but the data is stored once, so the space is freed.

- Linked paths are the *same file*: editing one changes all of them. Use this for
  files you don't edit (photos, music, installers, archives).
- The duplicate and the kept copy must be on the same drive, on a filesystem with hard
  links (NTFS, ext4, APFS, Btrfs, ZFS...). FAT32/exFAT USB drives and cross-drive pairs are
  skipped and listed as FAILED; nothing is changed for them.
- Contents are compared byte for byte right before linking, and the swap is atomic, so a
  failure never leaves a file missing.
- Linked files take the kept copy's modified date and permissions until restored.

## Web interface

```
python webapp.py                  # opens http://127.0.0.1:8765
python webapp.py --config FILE    # use another settings file
```

**Scan:** enter one or more folders (**+ Add folder**) and scan. Each duplicate set shows every copy with
its full location. Click a row to toggle Keep / the action, use "Keep only this", or
auto-select (oldest, newest, shortest path). The **Text** view shows the list as plain text
to copy or download. Choose at the bottom whether marked files are deleted, moved to the
dups folder, or replaced with hard links. Nothing changes until you confirm.

**History** and **Settings** work as described above.

The server refuses to delete every copy of a set, to touch files outside the scan,
or to act on a file whose size changed since the scan. It listens on 127.0.0.1 only and
requires a per-session token, so other websites can't drive it.

## How duplicates are detected

Files are grouped by size, then by a hash of the first 64 KB, then by a full SHA-256.
Only files identical byte for byte are reported. Empty files, symlinks and extra hard
links to the same data are ignored. The dups folder is always excluded from scans.

On Linux under WSL, Windows paths like `C:\Data` are translated to `/mnt/c/Data`.
