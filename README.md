# Cloud Duplicate Finder

A macOS desktop app (Tkinter) for finding duplicate photos across iCloud Drive,
Google Drive, and Dropbox, and cross-referencing them against a Capture One
catalog to see what's already imported.

## Features

- **Duplicate Finder** — scans local sync folders for iCloud Drive, Google
  Drive, and Dropbox (plus any custom folder you add) for `.heic`, `.jpg`,
  `.jpeg`, and `.png` files, groups exact duplicates by content hash, and lets
  you move extras to the Trash.
- **Capture One Cross-Reference** — finds `.cocatalog` files and checks which
  photos in your cloud folders are already referenced by Capture One (by
  filename) versus not yet imported.
- **Dropbox (Online)** — connects to your Dropbox account via OAuth and scans
  your *entire* Dropbox (not just what's synced to this Mac) for duplicates,
  using Dropbox's own content hash so nothing is downloaded except small
  thumbnail previews for files that turn out to have duplicates.
- **Google Drive (Online)** — same idea, via the Google Drive API and file
  checksums.

Both online tabs let you view a file on dropbox.com / drive.google.com, or
move duplicates to that service's own trash (recoverable, not a permanent
delete).

## Requirements

- macOS
- Python 3 with a **modern** Tk

Apple's system Python (`/usr/bin/python3` or the one bundled with Xcode) ships
an old Tk (8.5) that's known to render a **blank window** for `ttk` widgets on
current macOS versions. Use Homebrew's Python instead, which comes with Tk 9:

```
brew install python-tk@3.13
```

## Setup

```
git clone https://github.com/wiltzie75/CloudDuplicateFinder.git
cd CloudDuplicateFinder
/opt/homebrew/bin/python3.13 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

`pillow` and `pillow-heif` are optional (thumbnails in the Duplicate Finder
tab) but included by default; the other four packages are required for the
Dropbox and Google Drive tabs.

### Dropbox setup

The app needs its own Dropbox app so it isn't sharing a rate/user limit with
anyone else:

1. dropbox.com/developers/apps → **Create app**
2. API: **Scoped access**, Access type: **Full Dropbox**
3. **Permissions** tab → enable `files.metadata.read`, `files.content.read`,
   `files.content.write` → Submit
4. **Settings** tab → copy the **App key** and put it in `DROPBOX_APP_KEY` at
   the top of `cloud_duplicate_finder.py`
5. While the app is in Development status, only your own Dropbox account can
   connect. If you get "reached its user limit," click **Enable additional
   users** on the Settings tab.

The app authenticates via PKCE, so no app secret is ever needed at runtime —
don't add one to the script or commit one anywhere.

### Google Drive setup

Google requires its own registered OAuth client:

1. console.cloud.google.com → create or pick a project
2. **APIs & Services → Library** → enable the **Google Drive API**
3. **APIs & Services → OAuth consent screen** (or the **Audience** tab) → User
   type **External** → add your own Google account under **Test users**
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   Application type **Desktop app**
5. Download the resulting JSON and save it as:
   ```
   ~/.cloud_duplicate_finder_google_client.json
   ```
   (this path is intentionally outside the repo — it's a credential, not
   something to commit)

The consent screen stays in "Testing" mode indefinitely for personal use;
Google will show an "unverified app" warning on first connect — click
**Advanced → Go to (app name)** to proceed.

## Running

```
./.venv/bin/python3 cloud_duplicate_finder.py
```

Always launch it with the venv's Python, not a bare `python3` — that's what
guarantees the modern Tk and the installed packages.

## Building a standalone .app

```
./build_app.sh
```

Produces `Cloud Duplicate Finder.app`, double-clickable or draggable into
`/Applications`. The launcher it generates prefers this project's `.venv` if
present, so build it after setup, not before.

## Config & credentials on disk

- `~/.cloud_duplicate_finder.json` — Dropbox/Google refresh tokens (mode
  `600`, readable only by you)
- `~/.cloud_duplicate_finder_google_client.json` — the Google OAuth client you
  download yourself (see above)

Neither lives inside the repo, so there's nothing to `.gitignore` for them —
just don't move them into the project folder.

## Notes

- Local Trash and both services' "delete" actions are all recoverable (macOS
  Trash / Dropbox deleted-files / Google Drive trash), not permanent deletes.
- The Capture One catalog cross-reference matches by filename, not photo
  content — two unrelated photos that happen to share a filename would show
  as a false match.
