#!/usr/bin/env python3
"""
Cloud Duplicate Finder + Capture One Cross-Reference
------------------------------------------------------
Tab 1: Scans local sync folders for iCloud Drive, Google Drive, and Dropbox
       and finds duplicate files / wasted space.
Tab 2: Scans those same cloud folders for photos, and cross-references them
       against your Capture One catalog(s) to show which photos are already
       referenced in Capture One vs. not.

Run with:
    python3 cloud_duplicate_finder.py

Requires: Python 3 with tkinter (included with most Python installs;
on macOS with Homebrew Python you may need: brew install python-tk)

Optional (for image thumbnails in Tab 1):
    pip3 install pillow pillow-heif
"""

import os
import sys
import json
import sqlite3
import hashlib
import subprocess
import threading
import queue
import webbrowser
import urllib.parse
import urllib.request
import io
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pillow_heif  # optional: adds HEIC/HEIF support (common for iCloud Photos)
    pillow_heif.register_heif_opener()
except ImportError:
    pass

try:
    import dropbox
    from dropbox.oauth import DropboxOAuth2FlowNoRedirect
    from dropbox.files import FileMetadata
    DROPBOX_AVAILABLE = True
except ImportError:
    DROPBOX_AVAILABLE = False

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build as google_build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

HOME = Path.home()
SKIP_DIR_NAMES = {".git", "node_modules", ".Trash", ".Trashes", ".Spotlight-V100"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".heif", ".webp"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".raf", ".orf",
            ".rw2", ".pef", ".srw", ".x3f", ".3fr", ".iiq"}
PHOTO_EXTS = IMAGE_EXTS | RAW_EXTS
DUP_SCAN_EXTS = {".heic", ".jpg", ".jpeg", ".png"}
THUMB_SIZE = (48, 48)

# Dropbox app (public client identifier from the Dropbox App Console — not a
# secret). PKCE is used for auth, so no app secret is stored anywhere.
DROPBOX_APP_KEY = "uv9k0h9v3wrrgvp"
CONFIG_PATH = HOME / ".cloud_duplicate_finder.json"

# Google's own OAuth client for "Desktop app" credentials isn't treated as
# confidential (Google's docs: installed apps can't keep it secret), but it's
# still kept out of this file and out of the checked-in project — each user
# downloads their own from Google Cloud Console and it lives in their home dir.
GOOGLE_CLIENT_SECRET_PATH = HOME / ".cloud_duplicate_finder_google_client.json"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def human_size(n):
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def detect_services():
    """Best-effort detection of local sync folders for each cloud service."""
    found = {"iCloud Drive": [], "Google Drive": [], "Dropbox": []}

    icloud = HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.exists():
        found["iCloud Drive"].append(str(icloud))

    cloud_storage = HOME / "Library" / "CloudStorage"
    new_gdrive_roots = []
    if cloud_storage.exists():
        try:
            for entry in cloud_storage.iterdir():
                name = entry.name
                if name.startswith("GoogleDrive-"):
                    new_gdrive_roots.append(str(entry))
                    found["Google Drive"].append(str(entry))
                elif name.startswith("Dropbox"):
                    found["Dropbox"].append(str(entry))
        except OSError:
            pass

    for candidate in ["Dropbox", "Dropbox (Personal)", "Dropbox (Business)"]:
        p = HOME / candidate
        if p.exists() and str(p) not in found["Dropbox"]:
            found["Dropbox"].append(str(p))

    # Only add legacy ~/Google Drive if no new-style CloudStorage mount exists.
    # Both point to the same files; scanning both causes every Google Drive file
    # to appear as a duplicate of itself.
    if not new_gdrive_roots:
        legacy_gdrive = HOME / "Google Drive"
        if legacy_gdrive.exists():
            found["Google Drive"].append(str(legacy_gdrive))

    # Deduplicate within each service (e.g. multiple CloudStorage entries for
    # the same account can resolve to the same real path via symlinks)
    for service in found:
        seen_real = set()
        deduped = []
        for p in found[service]:
            real = os.path.realpath(p)
            if real not in seen_real:
                seen_real.add(real)
                deduped.append(p)
        found[service] = deduped

    return found


def iter_files(root_path, allowed_exts=None):
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")
        ]
        for fname in filenames:
            if fname == ".DS_Store":
                continue
            # iCloud placeholder for a file that hasn't been downloaded yet
            if fname.startswith(".") and fname.endswith(".icloud"):
                continue
            if allowed_exts is not None and Path(fname).suffix.lower() not in allowed_exts:
                continue
            full = os.path.join(dirpath, fname)
            try:
                if os.path.islink(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            if size == 0:
                continue
            yield full, size


def iter_image_files(root_path, exts):
    for full, size in iter_files(root_path):
        if Path(full).suffix.lower() in exts:
            yield full, size


# ---------------------------------------------------------------------------
# Duplicate-finding helpers
# ---------------------------------------------------------------------------

def quick_hash(path, size, chunk=65536):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(chunk))
            if size > chunk:
                f.seek(max(0, size - chunk))
                h.update(f.read(chunk))
    except OSError:
        return None
    return h.hexdigest()


def full_hash(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(paths, progress_cb=None, stop_flag=None):
    size_map = {}
    count = 0
    for base in paths:
        for full, size in iter_files(base, allowed_exts=DUP_SCAN_EXTS):
            if stop_flag and stop_flag.is_set():
                return None
            size_map.setdefault(size, []).append(full)
            count += 1
            if progress_cb and count % 250 == 0:
                progress_cb(f"Scanned {count} files...")

    candidates = {s: files for s, files in size_map.items() if len(files) > 1}

    quick_map = {}
    for size, files in candidates.items():
        for f in files:
            if stop_flag and stop_flag.is_set():
                return None
            qh = quick_hash(f, size)
            if qh is None:
                continue
            quick_map.setdefault((size, qh), []).append(f)

    dup_groups = []
    for (size, _qh), files in quick_map.items():
        if len(files) < 2:
            continue
        full_map = {}
        for f in files:
            fh = full_hash(f)
            if fh is None:
                continue
            full_map.setdefault(fh, []).append(f)
        for flist in full_map.values():
            if len(flist) > 1:
                wasted = size * (len(flist) - 1)
                dup_groups.append({"size": size, "files": sorted(flist), "wasted": wasted})

    dup_groups.sort(key=lambda g: g["wasted"], reverse=True)
    return dup_groups, count


def make_thumbnail(path):
    """Return a small ImageTk.PhotoImage thumbnail for an image file, or None."""
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(path)
        img.thumbnail(THUMB_SIZE)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        return ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"[thumbnail error] {path}: {e}")
        return None


def move_to_trash(path):
    """Move a file to macOS Trash (recoverable), via Finder/AppleScript."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{escaped}"'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, (e.stderr.decode() if e.stderr else str(e))
    except FileNotFoundError:
        return False, "osascript not available (non-macOS system?)"


# ---------------------------------------------------------------------------
# Config + Dropbox (online) helpers
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)  # readable only by this user (holds a token)
    except OSError:
        pass


def make_dropbox_client(config):
    """Build a Dropbox client from a stored refresh token, or None if not connected."""
    if not DROPBOX_AVAILABLE:
        return None
    refresh = config.get("dropbox_refresh_token")
    if not refresh:
        return None
    app_key = config.get("dropbox_app_key") or DROPBOX_APP_KEY
    return dropbox.Dropbox(oauth2_refresh_token=refresh, app_key=app_key)


def list_dropbox_files(dbx, progress_cb=None, stop_flag=None):
    """Return a list of (path_display, size, content_hash) for every .jpg/.jpeg/.heic/.png file in Dropbox."""
    files = []
    result = dbx.files_list_folder("", recursive=True)
    while True:
        for entry in result.entries:
            if isinstance(entry, FileMetadata) and Path(entry.path_display).suffix.lower() in DUP_SCAN_EXTS:
                files.append((entry.path_display, entry.size, entry.content_hash))
        if progress_cb:
            progress_cb(f"Listed {len(files)} files...")
        if stop_flag and stop_flag.is_set():
            return None
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    return files


def find_dropbox_duplicates(dbx, progress_cb=None, stop_flag=None):
    """Group Dropbox files by Dropbox's own content_hash to find exact duplicates.

    No file contents are downloaded — content_hash is provided in the metadata.
    """
    files = list_dropbox_files(dbx, progress_cb=progress_cb, stop_flag=stop_flag)
    if files is None:
        return None

    hash_map = {}
    for path, size, chash in files:
        if not chash or size == 0:
            continue
        hash_map.setdefault(chash, []).append((path, size))

    dup_groups = []
    for chash, items in hash_map.items():
        if len(items) < 2:
            continue
        items.sort()
        size = items[0][1]
        wasted = size * (len(items) - 1)
        dup_groups.append({
            "size": size,
            "files": [p for p, _ in items],
            "wasted": wasted,
        })

    dup_groups.sort(key=lambda g: g["wasted"], reverse=True)
    return dup_groups, len(files)


def download_dropbox_thumbnail(dbx, path, size=THUMB_SIZE):
    """Fetch and resize a small preview image for a Dropbox file, or return None on failure."""
    if not PIL_AVAILABLE:
        return None
    try:
        _, response = dbx.files_get_thumbnail_v2(
            dropbox.files.PathOrLink.path(path),
            format=dropbox.files.ThumbnailFormat.jpeg,
            size=dropbox.files.ThumbnailSize.w128h128,
        )
        img = Image.open(io.BytesIO(response.content))
        img.thumbnail(size)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def attach_dropbox_thumbnails(dbx, dup_groups, progress_cb=None, stop_flag=None):
    """Replace each group's plain path list with (path, thumbnail PNG bytes-or-None) pairs."""
    count = 0
    total = sum(len(g["files"]) for g in dup_groups)
    for group in dup_groups:
        new_files = []
        for path in group["files"]:
            if stop_flag and stop_flag.is_set():
                return None
            thumb_bytes = download_dropbox_thumbnail(dbx, path)
            new_files.append((path, thumb_bytes))
            count += 1
            if progress_cb and count % 20 == 0:
                progress_cb(f"Fetched {count}/{total} thumbnails...")
        group["files"] = new_files
    return dup_groups


# ---------------------------------------------------------------------------
# Config + Google Drive (online) helpers
# ---------------------------------------------------------------------------

def make_google_credentials(config):
    """Build Google OAuth credentials from a stored refresh token, or None if not connected."""
    if not GOOGLE_AVAILABLE:
        return None
    refresh = config.get("google_refresh_token")
    if not refresh or not GOOGLE_CLIENT_SECRET_PATH.exists():
        return None
    try:
        with open(GOOGLE_CLIENT_SECRET_PATH) as f:
            client_info = json.load(f)["installed"]
    except (OSError, ValueError, KeyError):
        return None
    return GoogleCredentials(
        None,
        refresh_token=refresh,
        client_id=client_info["client_id"],
        client_secret=client_info["client_secret"],
        token_uri=client_info["token_uri"],
        scopes=GOOGLE_SCOPES,
    )


def make_google_drive_service(config):
    """Build a Google Drive API client from a stored refresh token, or None if not connected."""
    creds = make_google_credentials(config)
    if creds is None:
        return None
    return google_build("drive", "v3", credentials=creds, cache_discovery=False)


def list_google_drive_files(service, progress_cb=None, stop_flag=None):
    """Return (file_id, name, size, md5Checksum, thumbnailLink) for every .jpg/.jpeg/.heic/.png file
    with a checksum.

    Native Google Docs/Sheets/Slides have no md5Checksum and are skipped — there's
    no byte content to compare for those.
    """
    files = []
    page_token = None
    while True:
        if stop_flag and stop_flag.is_set():
            return None
        response = service.files().list(
            q="trashed = false",
            fields="nextPageToken, files(id, name, size, md5Checksum, thumbnailLink)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        for f in response.get("files", []):
            if Path(f["name"]).suffix.lower() not in DUP_SCAN_EXTS:
                continue
            md5 = f.get("md5Checksum")
            size = f.get("size")
            if md5 and size:
                files.append((f["id"], f["name"], int(size), md5, f.get("thumbnailLink")))
        if progress_cb:
            progress_cb(f"Listed {len(files)} files...")
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def find_google_drive_duplicates(service, progress_cb=None, stop_flag=None):
    """Group Google Drive files by Google's own md5Checksum to find exact duplicates.

    No file contents are downloaded — md5Checksum is provided in the metadata.
    """
    files = list_google_drive_files(service, progress_cb=progress_cb, stop_flag=stop_flag)
    if files is None:
        return None

    hash_map = {}
    for file_id, name, size, md5, thumb_link in files:
        hash_map.setdefault(md5, []).append((file_id, name, size, thumb_link))

    dup_groups = []
    for md5, items in hash_map.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[1])
        size = items[0][2]
        wasted = size * (len(items) - 1)
        dup_groups.append({
            "size": size,
            "files": [(fid, name, thumb_link) for fid, name, _, thumb_link in items],
            "wasted": wasted,
        })

    dup_groups.sort(key=lambda g: g["wasted"], reverse=True)
    return dup_groups, len(files)


def download_google_drive_thumbnail(creds, thumbnail_link, size=THUMB_SIZE):
    """Fetch and resize a Drive file's small preview image, or return None on failure.

    Uses the thumbnailLink from the file's metadata — a small preview, not the full file.
    """
    if not thumbnail_link or not PIL_AVAILABLE:
        return None
    try:
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        req = urllib.request.Request(thumbnail_link, headers={"Authorization": f"Bearer {creds.token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        img = Image.open(io.BytesIO(raw))
        img.thumbnail(size)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def attach_google_drive_thumbnails(creds, dup_groups, progress_cb=None, stop_flag=None):
    """Replace each file's thumbnailLink with downloaded+resized thumbnail PNG bytes (or None)."""
    count = 0
    total = sum(len(g["files"]) for g in dup_groups)
    for group in dup_groups:
        new_files = []
        for file_id, name, thumb_link in group["files"]:
            if stop_flag and stop_flag.is_set():
                return None
            thumb_bytes = download_google_drive_thumbnail(creds, thumb_link)
            new_files.append((file_id, name, thumb_bytes))
            count += 1
            if progress_cb and count % 20 == 0:
                progress_cb(f"Fetched {count}/{total} thumbnails...")
        group["files"] = new_files
    return dup_groups


# ---------------------------------------------------------------------------
# Capture One cross-reference helpers
# ---------------------------------------------------------------------------

def find_capture_one_catalogs(search_roots, progress_cb=None, stop_flag=None):
    """Look for .cocatalog packages under the given root folders."""
    catalogs = []
    seen = set()
    for root in search_roots:
        root = Path(root)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if stop_flag and stop_flag.is_set():
                return catalogs
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")
            ]
            for d in list(dirnames):
                if d.lower().endswith(".cocatalog"):
                    full = os.path.join(dirpath, d)
                    if full not in seen:
                        seen.add(full)
                        catalogs.append(full)
                        if progress_cb:
                            progress_cb(f"Found catalog: {d}")
                    dirnames.remove(d)  # don't descend into the package itself
    return catalogs


def extract_catalog_filenames(catalog_path):
    """Return a set of lowercase photo/RAW basenames referenced inside a .cocatalog package.

    Capture One's database schema isn't public, so this takes a schema-agnostic
    approach: it looks at every text column of every table and keeps any value
    that ends in a known photo/RAW extension. This catches filenames wherever
    they live in the catalog, at the cost of matching by filename rather than
    by guaranteed internal record type.
    """
    names = set()
    db_files = list(Path(catalog_path).glob("*.cocatalogdb"))
    if not db_files:
        return names
    db_path = db_files[0]
    conn = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        for table in tables:
            try:
                cols = cur.execute(f'PRAGMA table_info("{table}")').fetchall()
            except sqlite3.Error:
                continue
            text_cols = [
                c[1] for c in cols
                if (c[2] or "").upper() in ("TEXT", "VARCHAR", "CHAR", "CLOB", "")
            ]
            for col in text_cols:
                try:
                    rows = cur.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 200000'
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for (val,) in rows:
                    if not isinstance(val, str) or "." not in val:
                        continue
                    base = os.path.basename(val).lower()
                    ext = os.path.splitext(base)[1]
                    if ext in PHOTO_EXTS:
                        names.add(base)
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
            conn.close()
    return names


def run_cross_reference(cloud_paths, catalog_paths, progress_cb=None, stop_flag=None):
    catalog_sets = {}
    for cat in catalog_paths:
        if stop_flag and stop_flag.is_set():
            return None
        if progress_cb:
            progress_cb(f"Reading catalog database: {Path(cat).name}...")
        catalog_sets[cat] = extract_catalog_filenames(cat)

    all_names = set()
    for s in catalog_sets.values():
        all_names |= s

    matched = []
    unmatched = []
    count = 0
    for base in cloud_paths:
        for full, size in iter_image_files(base, PHOTO_EXTS):
            if stop_flag and stop_flag.is_set():
                return None
            count += 1
            if progress_cb and count % 200 == 0:
                progress_cb(f"Checked {count} photos...")
            name = os.path.basename(full).lower()
            if name in all_names:
                matched.append((full, size))
            else:
                unmatched.append((full, size))

    return {
        "matched": sorted(matched),
        "unmatched": sorted(unmatched),
        "catalog_sets": catalog_sets,
        "total_checked": count,
    }


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cloud Duplicate Finder")
        self.geometry("1020x680")
        self.minsize(820, 520)

        self.queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.dup_groups = []
        self.custom_paths = []
        self.service_paths = detect_services()
        self._thumb_refs = []  # keep PhotoImage references alive

        self.c1_queue = queue.Queue()
        self.c1_stop_flag = threading.Event()
        self.detected_catalogs = []
        self.custom_catalog_roots = []
        self.cross_ref_result = None

        self.dbx_queue = queue.Queue()
        self.dbx_stop_flag = threading.Event()
        self.config_data = load_config()
        self.dbx_flow = None
        self.dbx_dup_groups = []
        self._dbx_thumb_refs = []  # keep PhotoImage references alive

        self.gdrive_queue = queue.Queue()
        self.gdrive_stop_flag = threading.Event()
        self.gdrive_dup_groups = []
        self.gdrive_item_info = {}
        self._gdrive_thumb_refs = []  # keep PhotoImage references alive

        style = ttk.Style(self)
        # Prefer macOS's native 'aqua' theme, but only when running as a bundled
        # .app (the launcher sets CDF_BUNDLED=1). As a loose script, aqua's
        # native buttons can be unresponsive to clicks because the process isn't
        # activated as a real app, so we fall back to 'clam' for reliability.
        bundled = os.environ.get("CDF_BUNDLED") == "1"
        tk_major = int(self.tk.call("info", "patchlevel").split(".")[0])
        if bundled and tk_major >= 9 and "aqua" in style.theme_names():
            theme = "aqua"
        else:
            theme = "clam"
        try:
            style.theme_use(theme)
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=56)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        tab_dup = ttk.Frame(notebook)
        tab_c1 = ttk.Frame(notebook)
        tab_dbx = ttk.Frame(notebook)
        tab_gdrive = ttk.Frame(notebook)
        notebook.add(tab_dup, text="Duplicate Finder")
        notebook.add(tab_c1, text="Capture One Cross-Reference")
        notebook.add(tab_dbx, text="Dropbox (Online)")
        notebook.add(tab_gdrive, text="Google Drive (Online)")

        self._build_dup_tab(tab_dup)
        self._build_c1_tab(tab_c1)
        self._build_dropbox_tab(tab_dbx)
        self._build_gdrive_tab(tab_gdrive)
        self._build_menubar()

        self.after(150, self._poll_queue)
        self.after(150, self._poll_c1_queue)
        self.after(150, self._poll_dbx_queue)
        self.after(150, self._poll_gdrive_queue)

    # ---- native macOS menu bar -------------------------------------------

    def _build_menubar(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="Scan for Duplicates", command=self._start_scan, accelerator="Cmd+R"
        )
        file_menu.add_command(
            label="Add Custom Folder…", command=self._add_custom, accelerator="Cmd+O"
        )
        menubar.add_cascade(label="File", menu=file_menu)

        # Keyboard shortcuts (macOS Command key)
        self.bind_all("<Command-r>", lambda e: self._start_scan())
        self.bind_all("<Command-o>", lambda e: self._add_custom())

        self.config(menu=menubar)

    # ---- shared tree selection helper ------------------------------------

    @staticmethod
    def _get_selected_files(tree):
        files = []
        for item in tree.selection():
            if tree.tag_has("file", item):
                files.append(tree.item(item, "values")[0])
        return files

    # ---- shared right-click context menu ---------------------------------

    def _bind_context_menu(self, tree, include_original=False):
        """Attach a right-click menu to a treeview.

        include_original adds a "Reveal Original" item that reveals the first
        file in the same group (the copy kept by 'Auto-select duplicates').
        """
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(
            label="Reveal in Finder",
            command=lambda: self._reveal(self._get_selected_files(tree)),
        )
        menu.add_command(
            label="Open File",
            command=lambda: self._open_files(self._get_selected_files(tree)),
        )
        if include_original:
            menu.add_separator()
            menu.add_command(
                label="Reveal Original (kept copy in group)",
                command=lambda: self._reveal_original(tree),
            )

        def popup(event):
            # Select the row under the cursor if it isn't already part of the
            # current selection, so the menu acts on what was right-clicked.
            row = tree.identify_row(event.y)
            if row and row not in tree.selection():
                tree.selection_set(row)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        # Button-2 is right-click on many Mac mice/trackpads; Button-3 elsewhere.
        for sequence in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            tree.bind(sequence, popup)

    def _open_files(self, files):
        if not files:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        for f in files[:10]:
            subprocess.run(["open", f])

    def _reveal_original(self, tree):
        """Reveal the first file of the group(s) the selection belongs to."""
        originals = []
        for item in tree.selection():
            if not tree.tag_has("file", item):
                continue
            group_id = tree.parent(item)
            if not group_id:
                continue
            siblings = tree.get_children(group_id)
            if siblings:
                first = tree.item(siblings[0], "values")
                if first and first[0] not in originals:
                    originals.append(first[0])
        if not originals:
            messagebox.showinfo("No original", "Select a duplicate file inside a group first.")
            return
        self._reveal(originals)

    # =======================================================================
    # TAB 1: Duplicate Finder
    # =======================================================================

    def _build_dup_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Detected locations", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(
            top, text="Scans only .heic, .jpg, .jpeg, and .png files.", foreground="gray"
        ).pack(anchor="w")

        services_frame = ttk.Frame(top)
        services_frame.pack(fill="x", pady=5)

        self.service_vars = {}
        for service, paths in self.service_paths.items():
            var = tk.BooleanVar(value=bool(paths))
            self.service_vars[service] = var
            label = f"{service} ({len(paths)} found)" if paths else f"{service} (not found)"
            cb = ttk.Checkbutton(
                services_frame, text=label, variable=var,
                state="normal" if paths else "disabled"
            )
            cb.pack(side="left", padx=(0, 16))

        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_frame, text="Add Custom Folder...", command=self._add_custom).pack(side="left")
        self.scan_btn = ttk.Button(btn_frame, text="Scan for Duplicates", command=self._start_scan)
        self.scan_btn.pack(side="left", padx=10)
        self.cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self._cancel_scan, state="disabled")
        self.cancel_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Ready. Select services above, then Scan.")
        ttk.Label(top, textvariable=self.status_var).pack(anchor="w", pady=(8, 0))

        self.summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.summary_var, font=("", 11, "bold")).pack(anchor="w")

        if not PIL_AVAILABLE:
            ttk.Label(
                top,
                text="Tip: run 'pip3 install pillow pillow-heif' to see image thumbnails here.",
                foreground="gray",
            ).pack(anchor="w", pady=(2, 0))

        mid = ttk.Frame(parent, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            mid, columns=("path", "size"), show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="Duplicate Group")
        self.tree.heading("path", text="Path")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=320)
        self.tree.column("path", width=460)
        self.tree.column("size", width=100, anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self._bind_context_menu(self.tree, include_original=True)

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Reveal in Finder",
                   command=lambda: self._reveal(self._get_selected_files(self.tree))).pack(side="left")
        ttk.Button(
            bottom, text="Auto-select duplicates (keep first)", command=self._auto_select
        ).pack(side="left", padx=10)
        ttk.Button(bottom, text="Move Selected to Trash", command=self._trash_selected).pack(side="left")

    def _add_custom(self):
        path = filedialog.askdirectory(title="Select folder to include in scan")
        if path:
            self.custom_paths.append(path)
            messagebox.showinfo("Added", f"Added custom folder:\n{path}")

    def _start_scan(self):
        paths = []
        for service, var in self.service_vars.items():
            if var.get():
                paths.extend(self.service_paths[service])
        paths.extend(self.custom_paths)

        if not paths:
            messagebox.showwarning("No folders selected", "Select at least one service or add a custom folder.")
            return

        self.stop_flag.clear()
        self.scan_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_var.set("Scanning... this can take a while for large clouds.")
        self.tree.delete(*self.tree.get_children())
        self.summary_var.set("")
        self._thumb_refs.clear()

        def progress(msg):
            self.queue.put(("progress", msg))

        def worker():
            result = find_duplicates(paths, progress_cb=progress, stop_flag=self.stop_flag)
            self.queue.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_scan(self):
        self.stop_flag.set()
        self.status_var.set("Cancelling...")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self.status_var.set(payload)
                elif kind == "done":
                    self._on_scan_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _on_scan_done(self, result):
        self.scan_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if result is None:
            self.status_var.set("Scan cancelled.")
            return
        dup_groups, total_scanned = result
        self.dup_groups = dup_groups
        total_wasted = sum(g["wasted"] for g in dup_groups)
        self.status_var.set(f"Scan complete — {total_scanned} files scanned.")
        self.summary_var.set(
            f"{len(dup_groups)} duplicate groups found — {human_size(total_wasted)} of wasted space"
        )

        for group in dup_groups:
            label = (
                f"{human_size(group['size'])} x {len(group['files'])} copies "
                f"— wastes {human_size(group['wasted'])}"
            )
            group_id = self.tree.insert("", "end", text=label, open=False, tags=("group",))
            for f in group["files"]:
                thumb = None
                if PIL_AVAILABLE and Path(f).suffix.lower() in IMAGE_EXTS:
                    thumb = make_thumbnail(f)
                    if thumb is not None:
                        self._thumb_refs.append(thumb)
                kwargs = dict(text="", values=(f, human_size(group["size"])), tags=("file",))
                if thumb is not None:
                    kwargs["image"] = thumb
                self.tree.insert(group_id, "end", **kwargs)

    def _reveal(self, files):
        if not files:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        for f in files[:10]:
            subprocess.run(["open", "-R", f])

    def _auto_select(self):
        self.tree.selection_remove(self.tree.selection())
        to_select = []
        for group_id in self.tree.get_children():
            children = self.tree.get_children(group_id)
            to_select.extend(children[1:])  # keep the first copy, select the rest
        self.tree.selection_set(to_select)
        self.status_var.set(f"Auto-selected {len(to_select)} duplicate copies (one original kept per group).")

    def _trash_selected(self):
        files = self._get_selected_files(self.tree)
        if not files:
            messagebox.showinfo("Nothing selected", "Select one or more duplicate files to trash.")
            return
        confirm = messagebox.askyesno(
            "Confirm",
            f"Move {len(files)} file(s) to the Trash?\n\nThis uses the macOS Trash, so it's recoverable "
            f"until you empty it."
        )
        if not confirm:
            return

        errors = []
        succeeded = set()
        for f in files:
            ok, err = move_to_trash(f)
            if ok:
                succeeded.add(f)
            else:
                errors.append(f"{f}: {err}")

        for item in list(self.tree.selection()):
            if self.tree.tag_has("file", item):
                values = self.tree.item(item, "values")
                if values and values[0] in succeeded:
                    self.tree.delete(item)

        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))
        else:
            messagebox.showinfo("Done", f"Moved {len(succeeded)} file(s) to Trash.")

    # =======================================================================
    # TAB 2: Capture One Cross-Reference
    # =======================================================================

    def _build_c1_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(
            top, text="Step 1 — Find your Capture One catalog(s)",
            font=("", 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            top,
            text="Searches ~/Pictures, ~/Documents, ~/Desktop, and your detected cloud folders for .cocatalog files.",
            foreground="gray"
        ).pack(anchor="w")

        cat_btn_frame = ttk.Frame(top)
        cat_btn_frame.pack(fill="x", pady=(6, 0))
        ttk.Button(cat_btn_frame, text="Find Catalogs", command=self._find_catalogs).pack(side="left")
        ttk.Button(cat_btn_frame, text="Add Custom Search Folder...",
                   command=self._add_catalog_root).pack(side="left", padx=10)

        self.catalog_list_frame = ttk.Frame(top)
        self.catalog_list_frame.pack(fill="x", pady=(6, 0))
        self.catalog_vars = {}

        ttk.Separator(top).pack(fill="x", pady=10)

        ttk.Label(
            top, text="Step 2 — Choose cloud folders to check, then cross-reference",
            font=("", 12, "bold")
        ).pack(anchor="w")

        c1_services_frame = ttk.Frame(top)
        c1_services_frame.pack(fill="x", pady=5)
        self.c1_service_vars = {}
        for service, paths in self.service_paths.items():
            var = tk.BooleanVar(value=bool(paths))
            self.c1_service_vars[service] = var
            label = f"{service} ({len(paths)} found)" if paths else f"{service} (not found)"
            cb = ttk.Checkbutton(
                c1_services_frame, text=label, variable=var,
                state="normal" if paths else "disabled"
            )
            cb.pack(side="left", padx=(0, 16))

        c1_btn_frame = ttk.Frame(top)
        c1_btn_frame.pack(fill="x", pady=(4, 0))
        self.c1_scan_btn = ttk.Button(
            c1_btn_frame, text="Cross-Reference Now", command=self._start_cross_reference
        )
        self.c1_scan_btn.pack(side="left")
        self.c1_cancel_btn = ttk.Button(
            c1_btn_frame, text="Cancel", command=self._cancel_cross_reference, state="disabled"
        )
        self.c1_cancel_btn.pack(side="left", padx=10)

        self.c1_status_var = tk.StringVar(value="Find your catalog(s) first, then cross-reference.")
        ttk.Label(top, textvariable=self.c1_status_var).pack(anchor="w", pady=(8, 0))

        self.c1_summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.c1_summary_var, font=("", 11, "bold")).pack(anchor="w")

        ttk.Label(
            top,
            text=("Note: matching is by filename, not photo content. Two different photos that happen to "
                  "share an identical filename could show as a false match."),
            foreground="gray", wraplength=900, justify="left"
        ).pack(anchor="w", pady=(4, 0))

        mid = ttk.Frame(parent, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.c1_tree = ttk.Treeview(
            mid, columns=("path", "size"), show="tree headings", selectmode="extended"
        )
        self.c1_tree.heading("#0", text="Group")
        self.c1_tree.heading("path", text="Path")
        self.c1_tree.heading("size", text="Size")
        self.c1_tree.column("#0", width=320)
        self.c1_tree.column("path", width=480)
        self.c1_tree.column("size", width=100, anchor="e")
        self.c1_tree.pack(side="left", fill="both", expand=True)

        c1_scrollbar = ttk.Scrollbar(mid, orient="vertical", command=self.c1_tree.yview)
        c1_scrollbar.pack(side="right", fill="y")
        self.c1_tree.configure(yscrollcommand=c1_scrollbar.set)

        self._bind_context_menu(self.c1_tree)

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Reveal in Finder",
                   command=lambda: self._reveal(self._get_selected_files(self.c1_tree))).pack(side="left")
        ttk.Button(
            bottom, text="Select all 'Already in Capture One'", command=self._select_matched
        ).pack(side="left", padx=10)
        ttk.Button(
            bottom, text="Move Selected to Trash", command=self._trash_selected_c1
        ).pack(side="left")

    def _find_catalogs(self):
        roots = [HOME / "Pictures", HOME / "Documents", HOME / "Desktop"]
        for paths in self.service_paths.values():
            roots.extend(paths)
        roots.extend(self.custom_catalog_roots)

        self.c1_status_var.set("Searching for .cocatalog files...")
        self.c1_scan_btn.config(state="disabled")

        def progress(msg):
            self.c1_queue.put(("find_progress", msg))

        def worker():
            cats = find_capture_one_catalogs(roots, progress_cb=progress, stop_flag=self.c1_stop_flag)
            self.c1_queue.put(("find_done", cats))

        threading.Thread(target=worker, daemon=True).start()

    def _add_catalog_root(self):
        path = filedialog.askdirectory(title="Select folder to search for Capture One catalogs")
        if path:
            self.custom_catalog_roots.append(path)
            messagebox.showinfo("Added", f"Will also search:\n{path}")

    def _populate_catalog_checkboxes(self, catalogs):
        for child in self.catalog_list_frame.winfo_children():
            child.destroy()
        self.catalog_vars = {}

        if not catalogs:
            ttk.Label(self.catalog_list_frame, text="No catalogs found.", foreground="gray").pack(anchor="w")
            return

        for cat in catalogs:
            var = tk.BooleanVar(value=True)
            self.catalog_vars[cat] = var
            ttk.Checkbutton(self.catalog_list_frame, text=Path(cat).name, variable=var).pack(anchor="w")

    def _start_cross_reference(self):
        cloud_paths = []
        for service, var in self.c1_service_vars.items():
            if var.get():
                cloud_paths.extend(self.service_paths[service])

        if not cloud_paths:
            messagebox.showwarning("No folders selected", "Select at least one cloud service to check.")
            return

        catalogs = [cat for cat, var in self.catalog_vars.items() if var.get()]
        if not catalogs:
            messagebox.showwarning(
                "No catalogs selected",
                "Click 'Find Catalogs' first and select at least one Capture One catalog."
            )
            return

        self.c1_stop_flag.clear()
        self.c1_scan_btn.config(state="disabled")
        self.c1_cancel_btn.config(state="normal")
        self.c1_status_var.set("Cross-referencing... this can take a while for large catalogs.")
        self.c1_tree.delete(*self.c1_tree.get_children())
        self.c1_summary_var.set("")

        def progress(msg):
            self.c1_queue.put(("xref_progress", msg))

        def worker():
            result = run_cross_reference(
                cloud_paths, catalogs, progress_cb=progress, stop_flag=self.c1_stop_flag
            )
            self.c1_queue.put(("xref_done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_cross_reference(self):
        self.c1_stop_flag.set()
        self.c1_status_var.set("Cancelling...")

    def _poll_c1_queue(self):
        try:
            while True:
                kind, payload = self.c1_queue.get_nowait()
                if kind == "find_progress":
                    self.c1_status_var.set(payload)
                elif kind == "find_done":
                    self.detected_catalogs = payload
                    self.c1_scan_btn.config(state="normal")
                    self._populate_catalog_checkboxes(payload)
                    self.c1_status_var.set(
                        f"Found {len(payload)} catalog(s)." if payload
                        else "No catalogs found. Try 'Add Custom Search Folder...'"
                    )
                elif kind == "xref_progress":
                    self.c1_status_var.set(payload)
                elif kind == "xref_done":
                    self._on_cross_reference_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_c1_queue)

    def _on_cross_reference_done(self, result):
        self.c1_scan_btn.config(state="normal")
        self.c1_cancel_btn.config(state="disabled")
        if result is None:
            self.c1_status_var.set("Cross-reference cancelled.")
            return

        self.cross_ref_result = result
        matched = result["matched"]
        unmatched = result["unmatched"]
        total = result["total_checked"]

        matched_size = sum(s for _, s in matched)
        unmatched_size = sum(s for _, s in unmatched)

        self.c1_status_var.set(f"Done — checked {total} photos across your cloud folders.")
        self.c1_summary_var.set(
            f"Already in Capture One: {len(matched)} photos ({human_size(matched_size)})   |   "
            f"Not yet in Capture One: {len(unmatched)} photos ({human_size(unmatched_size)})"
        )

        matched_group = self.c1_tree.insert(
            "", "end",
            text=f"Already in Capture One — {len(matched)} photos, {human_size(matched_size)}",
            open=False, tags=("group",)
        )
        for f, size in matched:
            self.c1_tree.insert(
                matched_group, "end", text="", values=(f, human_size(size)), tags=("file",)
            )

        unmatched_group = self.c1_tree.insert(
            "", "end",
            text=f"Not yet in Capture One — {len(unmatched)} photos, {human_size(unmatched_size)}",
            open=False, tags=("group",)
        )
        for f, size in unmatched:
            self.c1_tree.insert(
                unmatched_group, "end", text="", values=(f, human_size(size)), tags=("file",)
            )

    def _select_matched(self):
        """Select every file under the 'Already in Capture One' group."""
        self.c1_tree.selection_remove(self.c1_tree.selection())
        to_select = []
        for group_id in self.c1_tree.get_children():
            label = self.c1_tree.item(group_id, "text")
            if label.startswith("Already in Capture One"):
                to_select.extend(self.c1_tree.get_children(group_id))
        if not to_select:
            messagebox.showinfo("Nothing to select", "Run a cross-reference first.")
            return
        self.c1_tree.selection_set(to_select)
        self.c1_status_var.set(f"Selected {len(to_select)} photo(s) already in Capture One.")

    def _trash_selected_c1(self):
        files = self._get_selected_files(self.c1_tree)
        if not files:
            messagebox.showinfo("Nothing selected", "Select one or more photos to trash (or use 'Select all').")
            return
        confirm = messagebox.askyesno(
            "Confirm",
            f"Move {len(files)} photo(s) to the Trash?\n\n"
            f"These were matched as already being in Capture One by filename — double-check the list "
            f"before confirming, since this leaves Capture One's reference as the only remaining link.\n\n"
            f"Files go to the macOS Trash, so it's recoverable until you empty it."
        )
        if not confirm:
            return

        errors = []
        succeeded = set()
        for f in files:
            ok, err = move_to_trash(f)
            if ok:
                succeeded.add(f)
            else:
                errors.append(f"{f}: {err}")

        for item in list(self.c1_tree.selection()):
            if self.c1_tree.tag_has("file", item):
                values = self.c1_tree.item(item, "values")
                if values and values[0] in succeeded:
                    self.c1_tree.delete(item)

        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))
        else:
            messagebox.showinfo("Done", f"Moved {len(succeeded)} photo(s) to Trash.")


    # =======================================================================
    # TAB 3: Dropbox (Online)
    # =======================================================================

    def _build_dropbox_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(
            top, text="Connect your Dropbox account", font=("", 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            top,
            text=("Scans your entire Dropbox online for .jpg, .jpeg, .heic, and .png files, including "
                  "files not synced to this Mac. Duplicates are found using Dropbox's own content hash, "
                  "so nothing is downloaded."),
            foreground="gray", wraplength=900, justify="left"
        ).pack(anchor="w")

        if not DROPBOX_AVAILABLE:
            ttk.Label(
                top,
                text="Requires the 'dropbox' package. Run: pip3 install dropbox — then restart the app.",
                foreground="gray"
            ).pack(anchor="w", pady=(4, 0))

        conn_frame = ttk.Frame(top)
        conn_frame.pack(fill="x", pady=(8, 0))
        self.dbx_connect_btn = ttk.Button(
            conn_frame, text="Connect Dropbox...", command=self._connect_dropbox
        )
        self.dbx_connect_btn.pack(side="left")
        self.dbx_disconnect_btn = ttk.Button(
            conn_frame, text="Disconnect", command=self._disconnect_dropbox, state="disabled"
        )
        self.dbx_disconnect_btn.pack(side="left", padx=10)

        self.dbx_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(top, textvariable=self.dbx_status_var).pack(anchor="w", pady=(8, 0))

        ttk.Separator(top).pack(fill="x", pady=10)

        scan_frame = ttk.Frame(top)
        scan_frame.pack(fill="x")
        self.dbx_scan_btn = ttk.Button(
            scan_frame, text="Scan Dropbox for Duplicates", command=self._start_dropbox_scan, state="disabled"
        )
        self.dbx_scan_btn.pack(side="left")
        self.dbx_cancel_btn = ttk.Button(
            scan_frame, text="Cancel", command=self._cancel_dropbox_scan, state="disabled"
        )
        self.dbx_cancel_btn.pack(side="left", padx=10)

        self.dbx_summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.dbx_summary_var, font=("", 11, "bold")).pack(anchor="w", pady=(8, 0))

        mid = ttk.Frame(parent, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.dbx_tree = ttk.Treeview(
            mid, columns=("path", "size"), show="tree headings", selectmode="extended"
        )
        self.dbx_tree.heading("#0", text="Duplicate Group")
        self.dbx_tree.heading("path", text="Dropbox Path")
        self.dbx_tree.heading("size", text="Size")
        self.dbx_tree.column("#0", width=320)
        self.dbx_tree.column("path", width=460)
        self.dbx_tree.column("size", width=100, anchor="e")
        self.dbx_tree.pack(side="left", fill="both", expand=True)

        dbx_scrollbar = ttk.Scrollbar(mid, orient="vertical", command=self.dbx_tree.yview)
        dbx_scrollbar.pack(side="right", fill="y")
        self.dbx_tree.configure(yscrollcommand=dbx_scrollbar.set)

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(
            bottom, text="View in Dropbox.com",
            command=lambda: self._view_dropbox_paths(self._get_selected_files(self.dbx_tree))
        ).pack(side="left")
        ttk.Button(
            bottom, text="Delete Selected from Dropbox", command=self._delete_selected_dropbox
        ).pack(side="left", padx=10)

        self._update_dropbox_status()

    def _connect_dropbox(self):
        if not DROPBOX_AVAILABLE:
            messagebox.showerror(
                "Dropbox package not installed",
                "Run: pip3 install dropbox\nThen restart the app."
            )
            return

        flow = DropboxOAuth2FlowNoRedirect(DROPBOX_APP_KEY, use_pkce=True, token_access_type="offline")
        try:
            authorize_url = flow.start()
        except Exception as e:
            messagebox.showerror("Error starting authorization", str(e))
            return

        self.dbx_flow = flow
        webbrowser.open(authorize_url)
        code = simpledialog.askstring(
            "Dropbox Authorization",
            "A browser window opened to Dropbox's authorization page.\n\n"
            "1. Click Allow.\n"
            "2. Copy the code Dropbox shows you.\n"
            "3. Paste it below:",
            parent=self,
        )
        self.dbx_flow = None
        if not code:
            return

        try:
            result = flow.finish(code.strip())
        except Exception as e:
            messagebox.showerror("Authorization failed", str(e))
            return

        self.config_data["dropbox_refresh_token"] = result.refresh_token
        save_config(self.config_data)
        self._update_dropbox_status()

    def _disconnect_dropbox(self):
        confirm = messagebox.askyesno(
            "Disconnect Dropbox", "Remove the stored Dropbox connection from this app?"
        )
        if not confirm:
            return
        self.config_data.pop("dropbox_refresh_token", None)
        save_config(self.config_data)
        self.dbx_dup_groups = []
        self.dbx_tree.delete(*self.dbx_tree.get_children())
        self.dbx_summary_var.set("")
        self._update_dropbox_status()

    def _update_dropbox_status(self):
        dbx = make_dropbox_client(self.config_data)
        if dbx is None:
            self.dbx_status_var.set("Not connected.")
            self.dbx_connect_btn.config(text="Connect Dropbox...")
            self.dbx_scan_btn.config(state="disabled")
            self.dbx_disconnect_btn.config(state="disabled")
            return

        self.dbx_scan_btn.config(state="normal")
        self.dbx_disconnect_btn.config(state="normal")
        self.dbx_connect_btn.config(text="Reconnect Dropbox...")
        self.dbx_status_var.set("Connected — checking account...")

        def worker():
            try:
                acct = dbx.users_get_current_account()
                self.dbx_queue.put(("account", acct.email))
            except Exception as e:
                self.dbx_queue.put(("account_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_dropbox_scan(self):
        dbx = make_dropbox_client(self.config_data)
        if dbx is None:
            messagebox.showwarning("Not connected", "Connect your Dropbox account first.")
            return

        self.dbx_stop_flag.clear()
        self.dbx_scan_btn.config(state="disabled")
        self.dbx_cancel_btn.config(state="normal")
        self.dbx_status_var.set("Scanning Dropbox... this can take a while for large accounts.")
        self.dbx_tree.delete(*self.dbx_tree.get_children())
        self._dbx_thumb_refs.clear()
        self.dbx_summary_var.set("")

        def progress(msg):
            self.dbx_queue.put(("progress", msg))

        def worker():
            result = find_dropbox_duplicates(dbx, progress_cb=progress, stop_flag=self.dbx_stop_flag)
            if result is not None:
                dup_groups, total_listed = result
                dup_groups = attach_dropbox_thumbnails(
                    dbx, dup_groups, progress_cb=progress, stop_flag=self.dbx_stop_flag
                )
                result = None if dup_groups is None else (dup_groups, total_listed)
            self.dbx_queue.put(("scan_done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_dropbox_scan(self):
        self.dbx_stop_flag.set()
        self.dbx_status_var.set("Cancelling...")

    def _poll_dbx_queue(self):
        try:
            while True:
                kind, payload = self.dbx_queue.get_nowait()
                if kind == "progress":
                    self.dbx_status_var.set(payload)
                elif kind == "scan_done":
                    self._on_dropbox_scan_done(payload)
                elif kind == "account":
                    self.dbx_status_var.set(f"Connected as {payload}.")
                elif kind == "account_error":
                    self.dbx_status_var.set(f"Connected, but couldn't verify account: {payload}")
                elif kind == "delete_done":
                    self._on_dropbox_delete_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_dbx_queue)

    def _on_dropbox_scan_done(self, result):
        self.dbx_scan_btn.config(state="normal")
        self.dbx_cancel_btn.config(state="disabled")
        if result is None:
            self.dbx_status_var.set("Scan cancelled.")
            return

        dup_groups, total_listed = result
        self.dbx_dup_groups = dup_groups
        total_wasted = sum(g["wasted"] for g in dup_groups)
        self.dbx_status_var.set(f"Scan complete — {total_listed} files listed.")
        self.dbx_summary_var.set(
            f"{len(dup_groups)} duplicate groups found — {human_size(total_wasted)} of wasted space"
        )

        for group in dup_groups:
            label = (
                f"{human_size(group['size'])} x {len(group['files'])} copies "
                f"— wastes {human_size(group['wasted'])}"
            )
            group_id = self.dbx_tree.insert("", "end", text=label, open=False, tags=("group",))
            for path, thumb_bytes in group["files"]:
                kwargs = dict(text="", values=(path, human_size(group["size"])), tags=("file",))
                if thumb_bytes is not None:
                    try:
                        thumb = ImageTk.PhotoImage(Image.open(io.BytesIO(thumb_bytes)))
                        self._dbx_thumb_refs.append(thumb)
                        kwargs["image"] = thumb
                    except Exception:
                        pass
                self.dbx_tree.insert(group_id, "end", **kwargs)

    def _view_dropbox_paths(self, paths):
        if not paths:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        for p in paths[:10]:
            webbrowser.open("https://www.dropbox.com/home" + urllib.parse.quote(p))

    def _delete_selected_dropbox(self):
        paths = self._get_selected_files(self.dbx_tree)
        if not paths:
            messagebox.showinfo("Nothing selected", "Select one or more duplicate files to delete.")
            return
        dbx = make_dropbox_client(self.config_data)
        if dbx is None:
            messagebox.showwarning("Not connected", "Connect your Dropbox account first.")
            return
        confirm = messagebox.askyesno(
            "Confirm",
            f"Delete {len(paths)} file(s) from Dropbox?\n\n"
            f"This uses Dropbox's own deleted-files history, so it's recoverable from dropbox.com "
            f"for a limited time (30+ days depending on your plan)."
        )
        if not confirm:
            return

        self.dbx_status_var.set(f"Deleting {len(paths)} file(s) from Dropbox...")

        def worker():
            errors = []
            succeeded = []
            for p in paths:
                try:
                    dbx.files_delete_v2(p)
                    succeeded.append(p)
                except Exception as e:
                    errors.append(f"{p}: {e}")
            self.dbx_queue.put(("delete_done", (succeeded, errors)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_dropbox_delete_done(self, payload):
        succeeded, errors = payload
        succeeded_set = set(succeeded)
        for item in list(self.dbx_tree.selection()):
            if self.dbx_tree.tag_has("file", item):
                values = self.dbx_tree.item(item, "values")
                if values and values[0] in succeeded_set:
                    self.dbx_tree.delete(item)

        self.dbx_status_var.set(f"Deleted {len(succeeded)} file(s) from Dropbox.")
        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))

    # =======================================================================
    # TAB 4: Google Drive (Online)
    # =======================================================================

    def _build_gdrive_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(
            top, text="Connect your Google Drive account", font=("", 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            top,
            text=("Scans your entire Drive online for .jpg, .jpeg, .heic, and .png files, including files "
                  "not synced to this Mac. Duplicates are found using Google's own file checksum — full "
                  "files aren't downloaded, only small thumbnail previews for files with duplicates."),
            foreground="gray", wraplength=900, justify="left"
        ).pack(anchor="w")

        if not GOOGLE_AVAILABLE:
            ttk.Label(
                top,
                text=("Requires the Google API packages. Run: pip3 install google-api-python-client "
                      "google-auth-oauthlib google-auth-httplib2 — then restart the app."),
                foreground="gray"
            ).pack(anchor="w", pady=(4, 0))
        elif not GOOGLE_CLIENT_SECRET_PATH.exists():
            ttk.Label(
                top,
                text=(f"Missing OAuth client file. Download it from Google Cloud Console "
                      f"(APIs & Services -> Credentials) and save it as:\n{GOOGLE_CLIENT_SECRET_PATH}"),
                foreground="gray", wraplength=900, justify="left"
            ).pack(anchor="w", pady=(4, 0))

        conn_frame = ttk.Frame(top)
        conn_frame.pack(fill="x", pady=(8, 0))
        self.gdrive_connect_btn = ttk.Button(
            conn_frame, text="Connect Google Drive...", command=self._connect_google_drive
        )
        self.gdrive_connect_btn.pack(side="left")
        self.gdrive_disconnect_btn = ttk.Button(
            conn_frame, text="Disconnect", command=self._disconnect_google_drive, state="disabled"
        )
        self.gdrive_disconnect_btn.pack(side="left", padx=10)

        self.gdrive_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(top, textvariable=self.gdrive_status_var).pack(anchor="w", pady=(8, 0))

        ttk.Separator(top).pack(fill="x", pady=10)

        scan_frame = ttk.Frame(top)
        scan_frame.pack(fill="x")
        self.gdrive_scan_btn = ttk.Button(
            scan_frame, text="Scan Google Drive for Duplicates", command=self._start_gdrive_scan, state="disabled"
        )
        self.gdrive_scan_btn.pack(side="left")
        self.gdrive_cancel_btn = ttk.Button(
            scan_frame, text="Cancel", command=self._cancel_gdrive_scan, state="disabled"
        )
        self.gdrive_cancel_btn.pack(side="left", padx=10)

        self.gdrive_summary_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.gdrive_summary_var, font=("", 11, "bold")).pack(anchor="w", pady=(8, 0))

        mid = ttk.Frame(parent, padding=(10, 0, 10, 10))
        mid.pack(fill="both", expand=True)

        self.gdrive_tree = ttk.Treeview(
            mid, columns=("name", "size"), show="tree headings", selectmode="extended"
        )
        self.gdrive_tree.heading("#0", text="Duplicate Group")
        self.gdrive_tree.heading("name", text="File Name")
        self.gdrive_tree.heading("size", text="Size")
        self.gdrive_tree.column("#0", width=320)
        self.gdrive_tree.column("name", width=460)
        self.gdrive_tree.column("size", width=100, anchor="e")
        self.gdrive_tree.pack(side="left", fill="both", expand=True)

        gdrive_scrollbar = ttk.Scrollbar(mid, orient="vertical", command=self.gdrive_tree.yview)
        gdrive_scrollbar.pack(side="right", fill="y")
        self.gdrive_tree.configure(yscrollcommand=gdrive_scrollbar.set)

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="View in Google Drive", command=self._view_gdrive_selected).pack(side="left")
        ttk.Button(
            bottom, text="Move Selected to Drive Trash", command=self._delete_selected_gdrive
        ).pack(side="left", padx=10)

        self._update_gdrive_status()

    def _connect_google_drive(self):
        if not GOOGLE_AVAILABLE:
            messagebox.showerror(
                "Google API packages not installed",
                "Run: pip3 install google-api-python-client google-auth-oauthlib google-auth-httplib2\n"
                "Then restart the app."
            )
            return
        if not GOOGLE_CLIENT_SECRET_PATH.exists():
            messagebox.showerror(
                "Missing Google OAuth client",
                f"Expected the downloaded OAuth client file at:\n{GOOGLE_CLIENT_SECRET_PATH}"
            )
            return

        self.gdrive_connect_btn.config(state="disabled")
        self.gdrive_status_var.set("Waiting for Google sign-in in your browser...")

        def worker():
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT_SECRET_PATH), GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            except Exception as e:
                self.gdrive_queue.put(("connect_error", str(e)))
                return
            self.gdrive_queue.put(("connect_done", creds.refresh_token))

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect_google_drive(self):
        confirm = messagebox.askyesno(
            "Disconnect Google Drive", "Remove the stored Google Drive connection from this app?"
        )
        if not confirm:
            return
        self.config_data.pop("google_refresh_token", None)
        save_config(self.config_data)
        self.gdrive_dup_groups = []
        self.gdrive_tree.delete(*self.gdrive_tree.get_children())
        self.gdrive_item_info = {}
        self.gdrive_summary_var.set("")
        self._update_gdrive_status()

    def _update_gdrive_status(self):
        service = make_google_drive_service(self.config_data)
        if service is None:
            self.gdrive_status_var.set("Not connected.")
            self.gdrive_connect_btn.config(text="Connect Google Drive...", state="normal")
            self.gdrive_scan_btn.config(state="disabled")
            self.gdrive_disconnect_btn.config(state="disabled")
            return

        self.gdrive_scan_btn.config(state="normal")
        self.gdrive_disconnect_btn.config(state="normal")
        self.gdrive_connect_btn.config(text="Reconnect Google Drive...", state="normal")
        self.gdrive_status_var.set("Connected — checking account...")

        def worker():
            try:
                about = service.about().get(fields="user").execute()
                email = about.get("user", {}).get("emailAddress", "unknown")
                self.gdrive_queue.put(("account", email))
            except Exception as e:
                self.gdrive_queue.put(("account_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_gdrive_scan(self):
        creds = make_google_credentials(self.config_data)
        if creds is None:
            messagebox.showwarning("Not connected", "Connect your Google Drive account first.")
            return
        service = google_build("drive", "v3", credentials=creds, cache_discovery=False)

        self.gdrive_stop_flag.clear()
        self.gdrive_scan_btn.config(state="disabled")
        self.gdrive_cancel_btn.config(state="normal")
        self.gdrive_status_var.set("Scanning Google Drive... this can take a while for large accounts.")
        self.gdrive_tree.delete(*self.gdrive_tree.get_children())
        self.gdrive_item_info = {}
        self._gdrive_thumb_refs.clear()
        self.gdrive_summary_var.set("")

        def progress(msg):
            self.gdrive_queue.put(("progress", msg))

        def worker():
            result = find_google_drive_duplicates(service, progress_cb=progress, stop_flag=self.gdrive_stop_flag)
            if result is not None:
                dup_groups, total_listed = result
                dup_groups = attach_google_drive_thumbnails(
                    creds, dup_groups, progress_cb=progress, stop_flag=self.gdrive_stop_flag
                )
                result = None if dup_groups is None else (dup_groups, total_listed)
            self.gdrive_queue.put(("scan_done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_gdrive_scan(self):
        self.gdrive_stop_flag.set()
        self.gdrive_status_var.set("Cancelling...")

    def _poll_gdrive_queue(self):
        try:
            while True:
                kind, payload = self.gdrive_queue.get_nowait()
                if kind == "progress":
                    self.gdrive_status_var.set(payload)
                elif kind == "scan_done":
                    self._on_gdrive_scan_done(payload)
                elif kind == "account":
                    self.gdrive_status_var.set(f"Connected as {payload}.")
                elif kind == "account_error":
                    self.gdrive_status_var.set(f"Connected, but couldn't verify account: {payload}")
                elif kind == "connect_done":
                    self.gdrive_connect_btn.config(state="normal")
                    refresh_token = payload
                    if refresh_token:
                        self.config_data["google_refresh_token"] = refresh_token
                        save_config(self.config_data)
                        self._update_gdrive_status()
                    else:
                        self.gdrive_status_var.set(
                            "Connected, but Google didn't return a refresh token — try Disconnect then "
                            "Connect again."
                        )
                elif kind == "connect_error":
                    self.gdrive_connect_btn.config(state="normal")
                    self.gdrive_status_var.set(f"Connection failed: {payload}")
                elif kind == "delete_done":
                    self._on_gdrive_delete_done(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_gdrive_queue)

    def _on_gdrive_scan_done(self, result):
        self.gdrive_scan_btn.config(state="normal")
        self.gdrive_cancel_btn.config(state="disabled")
        if result is None:
            self.gdrive_status_var.set("Scan cancelled.")
            return

        dup_groups, total_listed = result
        self.gdrive_dup_groups = dup_groups
        total_wasted = sum(g["wasted"] for g in dup_groups)
        self.gdrive_status_var.set(f"Scan complete — {total_listed} files with checksums listed.")
        self.gdrive_summary_var.set(
            f"{len(dup_groups)} duplicate groups found — {human_size(total_wasted)} of wasted space"
        )

        for group in dup_groups:
            label = (
                f"{human_size(group['size'])} x {len(group['files'])} copies "
                f"— wastes {human_size(group['wasted'])}"
            )
            group_id = self.gdrive_tree.insert("", "end", text=label, open=False, tags=("group",))
            for file_id, name, thumb_bytes in group["files"]:
                kwargs = dict(text="", values=(name, human_size(group["size"])), tags=("file",))
                if thumb_bytes is not None:
                    try:
                        thumb = ImageTk.PhotoImage(Image.open(io.BytesIO(thumb_bytes)))
                        self._gdrive_thumb_refs.append(thumb)
                        kwargs["image"] = thumb
                    except Exception:
                        pass
                item = self.gdrive_tree.insert(group_id, "end", **kwargs)
                self.gdrive_item_info[item] = (file_id, name)

    def _get_selected_gdrive_files(self):
        return [
            self.gdrive_item_info[item]
            for item in self.gdrive_tree.selection()
            if item in self.gdrive_item_info
        ]

    def _view_gdrive_selected(self):
        items = self._get_selected_gdrive_files()
        if not items:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        for file_id, _name in items[:10]:
            webbrowser.open(f"https://drive.google.com/file/d/{file_id}/view")

    def _delete_selected_gdrive(self):
        items = self._get_selected_gdrive_files()
        if not items:
            messagebox.showinfo("Nothing selected", "Select one or more duplicate files to move to Trash.")
            return
        service = make_google_drive_service(self.config_data)
        if service is None:
            messagebox.showwarning("Not connected", "Connect your Google Drive account first.")
            return
        confirm = messagebox.askyesno(
            "Confirm",
            f"Move {len(items)} file(s) to Google Drive's Trash?\n\n"
            f"This is recoverable from drive.google.com for 30 days, after which Drive deletes it "
            f"permanently."
        )
        if not confirm:
            return

        item_by_file_id = {
            file_id: item
            for item, (file_id, _name) in self.gdrive_item_info.items()
        }
        self.gdrive_status_var.set(f"Moving {len(items)} file(s) to Google Drive Trash...")

        def worker():
            errors = []
            succeeded = []
            for file_id, name in items:
                try:
                    service.files().update(fileId=file_id, body={"trashed": True}).execute()
                    succeeded.append(item_by_file_id.get(file_id))
                except Exception as e:
                    errors.append(f"{name}: {e}")
            self.gdrive_queue.put(("delete_done", (succeeded, errors)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_gdrive_delete_done(self, payload):
        succeeded, errors = payload
        for item in succeeded:
            if item and self.gdrive_tree.exists(item):
                self.gdrive_tree.delete(item)
            self.gdrive_item_info.pop(item, None)

        self.gdrive_status_var.set(f"Moved {len(succeeded)} file(s) to Google Drive Trash.")
        if errors:
            messagebox.showerror("Some files failed", "\n".join(errors[:10]))


if __name__ == "__main__":
    print(f"[diagnostic] Python: {sys.executable}")
    print(f"[diagnostic] PIL_AVAILABLE: {PIL_AVAILABLE}")
    if sys.platform != "darwin":
        print("Note: Trash/Reveal actions are tuned for macOS. Scanning works cross-platform.")
    App().mainloop()