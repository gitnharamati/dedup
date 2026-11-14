"""
dedup_config.py - settings for Duplicate Finder, read from a JSON file.

Where the file is looked for, first match wins:
  1. --config PATH on the command line
  2. the DEDUP_CONFIG environment variable
  3. config.json next to the app

The file is optional: any setting it leaves out uses the default below.
Keys starting with "_" are ignored, so you can leave notes such as "_comment".
Relative paths in the file (dups_dir, history_file) are relative to the folder
that holds the config file.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = APP_ROOT / "config.json"

KEEP_STRATEGIES = ("oldest", "newest", "shortest")
CLI_ACTIONS = ("move", "link")
WEB_ACTIONS = ("delete", "move", "link")

DEFAULTS: dict[str, Any] = {
    "dups_dir": "dups",
    "history_file": "history.db",
    "keep": "oldest",
    "min_size": 1,
    "cli_action": "move",
    "web_action": "delete",
    "exclude_dirs": [".git", ".svn", ".hg", ".zfs", "@eaDir", "$RECYCLE.BIN",
                     "System Volume Information", ".Trash-*"],
    "exclude_files": ["Thumbs.db", "desktop.ini", ".DS_Store"],
    "skip_hidden": False,
    "write_report": True,
    "web_host": "127.0.0.1",
    "web_port": 8765,
    "open_browser": True,
}

# Settings the web page is allowed to change (host/port stay file-only).
WEB_EDITABLE = ("dups_dir", "keep", "min_size", "web_action", "cli_action",
                "exclude_dirs", "exclude_files", "skip_hidden", "write_report")


class ConfigError(ValueError):
    pass


def to_local_path(raw: str, base: Optional[Path] = None) -> Path:
    """Expand ~ and %VARS%, translate C:\\... to /mnt/c/... on Linux (WSL), make absolute."""
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
            raise ValueError(f"'{raw}' is a Windows path, but this is running on Linux/macOS. "
                             "Use the folder's Linux path instead.")
        s = str(wsl_mount / rest.replace("\\", "/"))
    elif s.startswith("\\\\"):
        raise ValueError("Network paths like \\\\server\\share only work on Windows; "
                         "mount the share and use its Linux path.")
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    return p.resolve()


def _check(key: str, value: Any) -> Any:
    """Validate one setting; return the cleaned value or raise ConfigError."""
    def need(cond, what):
        if not cond:
            raise ConfigError(f'"{key}" must be {what} (got {json.dumps(value)})')

    if key in ("dups_dir", "history_file", "web_host"):
        need(isinstance(value, str) and value.strip(), "a non-empty string")
        return value.strip()
    if key == "keep":
        need(value in KEEP_STRATEGIES, "one of " + ", ".join(KEEP_STRATEGIES))
    elif key == "cli_action":
        need(value in CLI_ACTIONS, "one of " + ", ".join(CLI_ACTIONS))
    elif key == "web_action":
        need(value in WEB_ACTIONS, "one of " + ", ".join(WEB_ACTIONS))
    elif key == "min_size":
        need(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
             "a whole number of bytes, 0 or more")
    elif key == "web_port":
        need(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535,
             "a port number between 1 and 65535")
    elif key in ("exclude_dirs", "exclude_files"):
        need(isinstance(value, list) and all(isinstance(v, str) for v in value),
             "a list of strings")
        return [v.strip() for v in value if v.strip()]
    elif key in ("skip_hidden", "write_report", "open_browser"):
        need(isinstance(value, bool), "true or false")
    return value


class Config:
    """Loaded settings. Read values with cfg["key"]; paths with cfg.dups_dir / cfg.history_file."""

    def __init__(self, path: Path, values: dict, warnings: list[str], exists: bool):
        self.path = path
        self.values = values
        self.warnings = warnings
        self.exists = exists

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def _resolve(self, raw: str) -> Path:
        return to_local_path(raw, base=self.path.parent)

    @property
    def dups_dir(self) -> Path:
        return self._resolve(self.values["dups_dir"])

    @property
    def history_file(self) -> Path:
        return self._resolve(self.values["history_file"])

    def as_dict(self) -> dict:
        return copy.deepcopy(self.values)


def config_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return to_local_path(explicit)
    env = os.environ.get("DEDUP_CONFIG")
    if env:
        return to_local_path(env)
    return DEFAULT_CONFIG_FILE


def _read_raw(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: line {e.lineno}, column {e.colno}: {e.msg}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object {{ ... }}")
    return data


def load_config(explicit: Optional[str] = None) -> Config:
    path = config_path(explicit)
    if explicit and not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = _read_raw(path)
    values = copy.deepcopy(DEFAULTS)
    warnings = []
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key not in DEFAULTS:
            warnings.append(f'{path.name}: unknown setting "{key}" ignored')
            continue
        try:
            values[key] = _check(key, value)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}")
    cfg = Config(path, values, warnings, path.exists())
    for key in ("dups_dir", "history_file"):        # catch unusable paths now, not mid-run
        try:
            getattr(cfg, key)
        except ValueError as e:
            raise ConfigError(f'{path}: "{key}": {e}')
    return cfg


def check_dups_dir(raw: str, base: Path) -> Path:
    """Resolve a dups folder, create it if needed, and make sure files can be written there."""
    p = to_local_path(raw, base)
    if p.exists() and not p.is_dir():
        raise ConfigError(f"Not a folder: {p}")
    try:
        p.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".dedup-write-test-", dir=p)
        os.close(fd)
        os.remove(probe)
    except OSError as e:
        raise ConfigError(f"Can't use {p} as the dups folder: {e.strerror or e}")
    return p


def save_settings(updates: dict, explicit: Optional[str] = None) -> Config:
    """Merge `updates` into the config file (creating it if needed) and return the new config.

    Settings already in the file that aren't being changed, including "_" notes, are kept.
    """
    path = config_path(explicit)
    raw = _read_raw(path)
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise ConfigError(f'Unknown setting "{key}"')
        value = _check(key, value)
        if key == "dups_dir":
            check_dups_dir(value, path.parent)
        raw[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return load_config(explicit)


def write_default_config(explicit: Optional[str] = None) -> Path:
    """Create a config file holding every default setting. Refuses to overwrite."""
    path = config_path(explicit)
    if path.exists():
        raise ConfigError(f"{path} already exists - edit it, or delete it first.")
    data = {"_comment": "Duplicate Finder settings. See README.md for what each one does."}
    data.update(DEFAULTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
