#!/usr/bin/env python3
"""
stickies_notes_sync.py

Menu bar app that performs a one-way sync of macOS Stickies notes into a
folder inside Apple Notes, preserving basic rich text formatting (v0.1 +
v0.2 scope: create / update / delete plain and rich-text notes). Two-way
sync and auto-creating new Stickies are intentionally out of scope.

See the accompanying README.md for setup instructions.
"""

import hashlib
import json
import logging
import logging.handlers
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import rumps
from PyObjCTools import AppHelper
import AppKit

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_NAME = "StickySync"

# Where config/state/logs live. Override with the STICKIES_SYNC_HOME env
# var if you want them somewhere else.
HOME_DIR = Path(
    os.environ.get(
        "STICKIES_SYNC_HOME",
        str(Path.home() / "Library" / "Application Support" / APP_NAME),
    )
).expanduser()

CONFIG_PATH = HOME_DIR / "config.json"
MAPPING_PATH = HOME_DIR / "mapping.json"
LOG_PATH = HOME_DIR / "sync.log"

DEFAULT_CONFIG = {
    # Where Stickies stores its notes. This is the layout used from macOS
    # Catalina through at least Sonoma (one .rtfd package per note) -
    # confirm this matches your machine before relying on it; see README.
    "stickies_container": str(
        Path.home()
        / "Library"
        / "Containers"
        / "com.apple.Stickies"
        / "Data"
        / "Library"
        / "Stickies"
    ),
    # The Notes.app account and folder to sync into. Use the exact names
    # shown in Notes.app's sidebar. "On My Mac" keeps these notes off
    # iCloud; use your iCloud account name instead if you want that.
    "notes_account": "iCloud",
    "notes_folder": "Stickies",
    # How often the menu bar app re-syncs, in minutes.
    "sync_interval_minutes": 5,
    "log_level": "INFO",
}


def load_config() -> dict:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        return dict(DEFAULT_CONFIG)
    try:
        user_config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse {CONFIG_PATH}: {exc}")
    merged = dict(DEFAULT_CONFIG)
    merged.update(user_config)
    return merged


CONFIG = load_config()

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger(APP_NAME)
logger.setLevel(getattr(logging, CONFIG.get("log_level", "INFO"), logging.INFO))
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)


# --------------------------------------------------------------------------
# AppleScript helpers (talking to Notes.app)
# --------------------------------------------------------------------------

def _as_literal(value: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def ensure_notes_folder(account: str, folder: str) -> None:
    script = f'''
    tell application "Notes"
        launch
        tell account "{_as_literal(account)}"
            if not (exists folder "{_as_literal(folder)}") then
                make new folder with properties {{name:"{_as_literal(folder)}"}}
            end if
        end tell
    end tell
    '''
    _run_applescript(script)


def create_note(account: str, folder: str, html_path: Path) -> str:
    script = f'''
    set htmlText to (read (POSIX file "{_as_literal(str(html_path))}") as «class utf8»)
    tell application "Notes"
        launch
        tell account "{_as_literal(account)}"
            tell folder "{_as_literal(folder)}"
                set newNote to make new note with properties {{body:htmlText}}
            end tell
        end tell
        return id of newNote
    end tell
    '''
    return _run_applescript(script)


def update_note(note_id: str, html_path: Path) -> bool:
    """Returns True if the note was updated, False if it no longer exists
    (or is in Recently Deleted)."""
    script = f'''
    set htmlText to (read (POSIX file "{_as_literal(str(html_path))}") as «class utf8»)
    tell application "Notes"
        launch
        try
            set theNote to note id "{_as_literal(note_id)}"
        on error
            return "MISSING"
        end try
        try
            set body of theNote to htmlText
        on error errMsg number errNum
            -- Notes in "Recently Deleted" still resolve by id but are read-only.
            set noteId to id of theNote
            repeat with acct in accounts
                if exists folder "Recently Deleted" of acct then
                    if (id of notes of folder "Recently Deleted" of acct) contains noteId then return "MISSING"
                end if
            end repeat
            error errMsg number errNum
        end try
        return "OK"
    end tell
    '''
    return _run_applescript(script) == "OK"


def delete_note(note_id: str) -> None:
    script = f'''
    tell application "Notes"
        launch
        try
            delete (note id "{_as_literal(note_id)}")
        end try
    end tell
    '''
    _run_applescript(script)


# --------------------------------------------------------------------------
# Reading Stickies
# --------------------------------------------------------------------------

def read_stickies(container: Path) -> dict:
    """Returns {uuid: html_string} for every note currently in Stickies."""
    notes = {}
    if not container.is_dir():
        logger.error("Stickies folder not found: %s", container)
        return notes

    entries = sorted(container.glob("*.rtfd"))
    if not entries:
        # The on-disk format has changed across macOS versions before.
        # Log what's actually there so a mismatch is easy to diagnose
        # instead of silently syncing nothing.
        seen = [p.name for p in container.iterdir()]
        logger.warning(
            "No .rtfd notes found in %s. Directory contains: %s", container, seen
        )
        return notes

    for entry in entries:
        uuid = entry.stem
        try:
            result = subprocess.run(
                ["textutil", "-convert", "html", "-stdout", str(entry)],
                capture_output=True,
                text=True,
                check=True,
            )
            notes[uuid] = result.stdout
        except subprocess.CalledProcessError as exc:
            logger.error("Failed to read %s: %s", entry, exc.stderr)
    return notes


def content_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Mapping store (Stickies UUID <-> Notes id, plus last-synced hash)
# --------------------------------------------------------------------------

def load_mapping() -> dict:
    if MAPPING_PATH.exists():
        try:
            return json.loads(MAPPING_PATH.read_text())
        except json.JSONDecodeError:
            logger.error("Mapping file corrupt, starting fresh: %s", MAPPING_PATH)
    return {}


def save_mapping(mapping: dict) -> None:
    tmp = MAPPING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(mapping, indent=2))
    tmp.replace(MAPPING_PATH)


# --------------------------------------------------------------------------
# Sync engine
# --------------------------------------------------------------------------

def run_sync() -> dict:
    stats = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "errors": 0}

    notes_was_running = _run_applescript('return application "Notes" is running') == "true"

    try:
        container = Path(CONFIG["stickies_container"]).expanduser()
        account = CONFIG["notes_account"]
        folder = CONFIG["notes_folder"]

        try:
            ensure_notes_folder(account, folder)
        except Exception as exc:
            logger.error("Could not ensure Notes folder exists: %s", exc)
            stats["errors"] += 1
            return stats

        current = read_stickies(container)
        mapping = load_mapping()

        # Deletions: mapped notes whose Stickies file is gone.
        for uuid in list(mapping):
            if uuid not in current:
                try:
                    delete_note(mapping[uuid]["notes_id"])
                    stats["deleted"] += 1
                except Exception as exc:
                    logger.error("Failed to delete note for %s: %s", uuid, exc)
                    stats["errors"] += 1
                del mapping[uuid]

        # Creates and updates.
        for uuid, html in current.items():
            digest = content_hash(html)
            entry = mapping.get(uuid)

            with tempfile.NamedTemporaryFile(
                "w", suffix=".html", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(html)
                html_path = Path(tmp.name)

            try:
                if entry is None:
                    note_id = create_note(account, folder, html_path)
                    mapping[uuid] = {
                        "notes_id": note_id,
                        "content_hash": digest,
                        "last_synced": datetime.now(timezone.utc).isoformat(),
                    }
                    stats["created"] += 1
                elif entry["content_hash"] != digest:
                    updated = update_note(entry["notes_id"], html_path)
                    if not updated:
                        # Note was deleted or moved on the Notes side; recreate it.
                        note_id = create_note(account, folder, html_path)
                        mapping[uuid]["notes_id"] = note_id
                    mapping[uuid]["content_hash"] = digest
                    mapping[uuid]["last_synced"] = datetime.now(timezone.utc).isoformat()
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
            except Exception as exc:
                logger.error("Sync failed for %s: %s", uuid, exc)
                stats["errors"] += 1
            finally:
                html_path.unlink(missing_ok=True)

        save_mapping(mapping)
        logger.info("Sync complete: %s", stats)
        return stats
    finally:
        if not notes_was_running:
            try:
                _run_applescript('tell application "Notes" to quit')
            except Exception as exc:
                logger.error("Failed to quit Notes after sync: %s", exc)


# --------------------------------------------------------------------------
# Menu bar app
# --------------------------------------------------------------------------

class StickiesNotesSyncApp(rumps.App):
    def __init__(self):
        super().__init__("🗒", quit_button=None)
        self.sync_lock = threading.Lock()
        self.status_item = rumps.MenuItem("Last synced: never")
        self.menu = [
            rumps.MenuItem("Sync now", callback=self.sync_now),
            None,
            self.status_item,
            None,
            rumps.MenuItem("Open config", callback=self.open_config),
            rumps.MenuItem("Open log", callback=self.open_log),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        interval_seconds = max(1, int(CONFIG.get("sync_interval_minutes", 5))) * 60
        self.timer = rumps.Timer(self.scheduled_sync, interval_seconds)
        self.timer.start()
        # Run once immediately at launch rather than waiting for the first tick.
        self.scheduled_sync(None)

    def _update_ui_success(self, stats, now):
        if stats["errors"]:
            self.title = "🗒⚠"
            self.status_item.title = (
                f"Last synced {now} — {stats['errors']} error(s), see log"
            )
            rumps.notification(
                APP_NAME,
                "Sync completed with errors",
                f"{stats['errors']} note(s) failed - check the log.",
            )
        else:
            self.title = "🗒"
            self.status_item.title = (
                f"Last synced {now} — "
                f"+{stats['created']} ~{stats['updated']} -{stats['deleted']}"
            )

    def _update_ui_error(self, exc_str):
        self.title = "🗒⚠"
        self.status_item.title = f"Sync failed: {exc_str}"

    def _do_sync(self):
        if not self.sync_lock.acquire(blocking=False):
            return
        try:
            stats = run_sync()
            now = datetime.now().strftime("%H:%M:%S")
            AppHelper.callAfter(self._update_ui_success, stats, now)
        except Exception as exc:
            logger.exception("Unexpected sync failure")
            AppHelper.callAfter(self._update_ui_error, str(exc))
        finally:
            self.sync_lock.release()

    def scheduled_sync(self, _sender):
        threading.Thread(target=self._do_sync, daemon=True).start()

    def sync_now(self, _sender):
        self.scheduled_sync(_sender)

    def open_config(self, _sender):
        subprocess.run(["open", str(CONFIG_PATH)])

    def open_log(self, _sender):
        subprocess.run(["open", str(LOG_PATH)])


if __name__ == "__main__":
    app = StickiesNotesSyncApp()
    import AppKit
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app.run()