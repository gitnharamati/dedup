# Duplicate Finder

Two tools that find files with identical content (Python 3.8+, standard library only, Windows or Linux).

Keep all four files in one folder:

| File | What it is |
|---|---|
| `dedup.py` | Command line: finds duplicates and moves the extra copies to `dups/` |
| `webapp.py` + `index.html` | Local web page to review duplicates and pick what to keep or delete |
| `dedup_core.py` | Shared scanning code used by both |

## Command line

```
python dedup.py "C:\Users\me\Pictures"        # move duplicates, keep the oldest copy
python dedup.py /home/me/Pictures --dry-run   # only list them
python dedup.py D:\Music --keep shortest      # keep: oldest | newest | shortest (path)
python dedup.py D:\Music --link               # replace duplicates with hard links
python dedup.py --restore                     # undo moves: move everything back
```

For each duplicate set, one copy stays put and the rest go to `dups/` next to the script,
with the source folder added to the name:

```
C:\Photos\backup\2020\img.jpg  ->  dups\img [Photos_backup_2020].jpg
```

Every run writes `dups/report_<date>.txt` (the full list of duplicates and where they are)
and appends to `dups/manifest.csv`, which `--restore` uses.

### Hard links (`--link`)

Instead of moving duplicates, `--link` replaces each one with a hard link to the kept
copy. Every path keeps working, but the data is stored once, so the space is freed.

- Linked paths are the *same file*: editing one changes all of them. Use this for
  files you don't edit (photos, music, installers, archives).
- The duplicate and the kept copy must be on the same drive, on a filesystem with hard
  links (NTFS, ext4, APFS, Btrfs...). FAT32/exFAT USB drives and cross-drive pairs are
  skipped and listed as FAILED; nothing is changed for them.
- Contents are compared byte for byte right before linking, and the swap is atomic, so a
  failure never leaves a file missing.
- Linked files take the kept copy's modified date and permissions.
- `--restore` doesn't undo links. To separate a file again, copy it and replace the original with the copy.

## Web interface

```
python webapp.py        # opens http://127.0.0.1:8765
```

Enter a folder and scan. Each duplicate set shows every copy with its full location.
Click a row to toggle Keep / Delete, use "Keep only this", or auto-select (oldest, newest,
shortest path). The **Text** tab shows the same list as plain text, which you can copy
or download. Choose what happens to marked files at the bottom of the page: delete
permanently, move to the `dups` folder, or replace with a hard link to a kept copy in
the same set. Nothing changes until you press the action button and confirm.

The server refuses to delete every copy of a set, to touch files outside the scan,
or to delete a file whose size changed since the scan. It listens on 127.0.0.1 only.

## How duplicates are detected

Files are grouped by size, then by a hash of the first 64 KB, then by a full SHA-256.
Only files identical byte for byte are reported. Empty files, symlinks and extra hard
links to the same data are ignored. The `dups/` folder is excluded from scans.

On Linux under WSL, Windows paths like `C:\Data` are translated to `/mnt/c/Data`.
