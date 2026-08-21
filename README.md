# Stickies → Notes sync (v0.1 / v0.2)

A menu bar app that does a one-way sync of macOS Stickies notes into a folder
in Apple Notes, preserving basic rich text formatting. Create/update/delete
on the Stickies side is reflected in Notes. Nothing flows back the other way.

## 1. Verify the Stickies file layout on your Mac

This script assumes each Stickies note is stored as its own `.rtfd` package
in:

```
~/Library/Containers/com.apple.Stickies/Data/Library/Stickies/
```

This has been the layout since macOS Catalina, but Apple has changed it
before and doesn't document it. Before installing anything, check it
yourself:

1. In Finder, press `Cmd+Shift+G` and paste the path above.
2. You should see one `.rtfd` folder per sticky note, named with a UUID.

If you see something different (e.g. a single `StickiesDatabase` file, no
container folder at all), stop here — the script won't work as-is and the
`stickies_container` path in the config will need to point somewhere else,
or the reading logic will need adjusting. This is the one part of the
project genuinely tied to macOS version/undocumented behavior.

## 2. Install Python and rumps

Recommended: use Homebrew's Python rather than the system one, so you get a
clean, writable environment.

```bash
brew install python3   # skip if you already have Python 3.9+

mkdir -p ~/Developer/stickies-notes-sync
cd ~/Developer/stickies-notes-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy `stickies_notes_sync.py` and `requirements.txt` into that folder.

## 3. Run it manually the first time

Do this step before setting up auto-start — the permission prompts below
need an interactive foreground session to appear reliably.

```bash
python3 stickies_notes_sync.py
```

A menu bar icon (🗒) should appear. This first run also creates a config
file at:

```
~/Library/Application Support/StickiesNotesSync/config.json
```

Open it (`open ~/Library/Application\ Support/StickiesNotesSync/config.json`)
and adjust:

- `stickies_container` — only if step 1 found a different path.
- `notes_account` — the exact name shown in Notes.app's sidebar, e.g.
  `"On My Mac"` or your iCloud account name. `"On My Mac"` avoids pushing
  these notes through iCloud sync.
- `notes_folder` — the folder name to create/use inside that account.
- `sync_interval_minutes` — how often it re-syncs while running.

Restart the script after editing the config (`Ctrl+C`, then run again).

### Grant permissions

The first sync will trigger one or two macOS permission prompts:

- **Automation** — "wants to control Notes.app" (or similar). Approve it.
  If you don't see a prompt and syncing silently fails, check
  **System Settings → Privacy & Security → Automation**.
- **Full Disk Access** — required to read Stickies' container folder,
  which belongs to a different app. Go to
  **System Settings → Privacy & Security → Full Disk Access**, click `+`,
  press `Cmd+Shift+G`, and add the exact python3 binary you're running —
  find it with `which python3` while your venv is active (it'll be
  something like `.../stickies-notes-sync/venv/bin/python3`, not
  `/usr/bin/python3`). Quit and restart the script after granting this.

### Verify it worked

- Click the menu bar icon → **Sync now**.
- Check Notes.app for the new folder and notes.
- If something's wrong, click **Open log** from the menu, or check
  `~/Library/Application Support/StickiesNotesSync/sync.log` directly.

If notes are created but formatting looks off, or nothing shows up at all
while the log says "No .rtfd notes found... Directory contains: [...]",
that's the format-mismatch case from step 1 — the log will show you what's
actually in the folder.

## 4. Auto-start at login

Once the manual run works end-to-end, set it up to start automatically.

Edit `com.local.stickiesnotessync.plist`: replace every `/Users/YOURNAME/...`
placeholder with the real absolute paths on your Mac (launchd doesn't
expand `~`). The `ProgramArguments` python3 path must be the same venv
binary you granted Full Disk Access to above.

Copy it into place and load it:

```bash
cp com.local.stickiesnotessync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.stickiesnotessync.plist
```

(On very recent macOS, `launchctl` may prefer the newer syntax:
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.stickiesnotessync.plist`
— try `load` first, fall back to `bootstrap` if it errors.)

The menu bar icon should now appear within a few seconds, and again
automatically every time you log in.

To stop auto-start:

```bash
launchctl unload ~/Library/LaunchAgents/com.local.stickiesnotessync.plist
rm ~/Library/LaunchAgents/com.local.stickiesnotessync.plist
```

## Known limitations (by design, v0.2 scope)

- One-way only: edits made directly in Notes are ignored and will be
  overwritten on the next sync if the corresponding Stickies note also
  changes.
- Images/attachments pasted into a sticky note aren't specifically
  handled — text formatting (bold, italic, underline, lists, color) is
  the target; embedded images may not carry over cleanly.
- If you rename/delete the Notes folder manually, the script will recreate
  it, but existing notes it can no longer find by id will be recreated
  rather than matched back up.

## Uninstalling

```bash
launchctl unload ~/Library/LaunchAgents/com.local.stickiesnotessync.plist
rm ~/Library/LaunchAgents/com.local.stickiesnotessync.plist
rm -rf ~/Library/Application\ Support/StickiesNotesSync
rm -rf ~/Developer/stickies-notes-sync
```

Then remove the app's entries under Full Disk Access and Automation in
System Settings → Privacy & Security.