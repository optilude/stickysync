# AGENTS.md

## Project

StickySync – macOS menu bar app that one-way syncs Stickies notes into Apple Notes.

## Key files

- `stickysync.py` – single entrypoint (run directly)
- `requirements.txt` – only `rumps>=0.4.0`
- `com.local.stickiesnotessync.plist` – launchd auto-start template

## Running

```bash
python3 stickysync.py
```

No build step. No test suite. Direct execution only.

## Configuration

- Config: `~/Library/Application Support/StickySync/config.json` (auto-created on first run)
- Mapping state: `~/Library/Application Support/StickySync/mapping.json`
- Logs: `~/Library/Application Support/StickySync/sync.log`
- Override config location via `STICKIES_SYNC_HOME` env var

## macOS permissions required

- **Full Disk Access** – must be granted to the exact Python binary running the script (e.g., venv's `python3`), not `/usr/bin/python3`
- **Automation** – "control Notes.app" prompt appears on first sync

## Architecture notes

- Uses `rumps` for menu bar UI
- Talks to Notes.app via AppleScript through `osascript` subprocess
- Converts Stickies' `.rtfd` packages to HTML using `textutil -convert html -stdout`
- Stickies container path (`~/Library/Containers/com.apple.Stickies/Data/Library/Stickies/`) is macOS-version-specific (Catalina through Sonoma); may differ on other versions

## Deployment

The launchd plist requires absolute paths (no `~` expansion). The `ProgramArguments` Python path must match the binary granted Full Disk Access.
