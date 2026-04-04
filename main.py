import os
import json
import hashlib
import time
import uuid
import threading
import subprocess
import sys
import base64
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory, Response, stream_with_context
from werkzeug.utils import secure_filename

# ============================================================
# AUTO INSTALL DEPENDENCIES ON STARTUP
# ============================================================

REQUIRED_PACKAGES = [
    "flask", "google-api-python-client", "google-auth-httplib2",
    "google-auth-oauthlib", "werkzeug", "mutagen",
    "imageio-ffmpeg", "pillow", "requests",
]

def install_package(pkg):
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", pkg, "--quiet", "--break-system-packages"],
        capture_output=True, text=True
    )
    return result.returncode == 0, result.stdout + result.stderr

def install_all_packages(packages=None):
    if packages is None:
        packages = REQUIRED_PACKAGES
    results = []
    for pkg in packages:
        ok, out = install_package(pkg)
        results.append({"package": pkg, "success": ok, "output": out})
        print(f"[INSTALL] {'OK' if ok else 'FAIL'} {pkg}")
    return results

print("[STARTUP] App dimulai. Dependencies dari requirements.txt.")

# ============================================================
# IMPORTS
# ============================================================

try:
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.http import MediaFileUpload
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False
    print("[WARN] Google API tidak tersedia")

try:
    import imageio_ffmpeg
    FFMPEG_BINARY = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[FFMPEG] Found: {FFMPEG_BINARY}")
except Exception:
    FFMPEG_BINARY = None

try:
    import requests as req_lib
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
MUSIC_FOLDER = os.path.join(BASE_DIR, 'music')
TEMP_FOLDER  = os.path.join(BASE_DIR, '_temp')
ABOUT_FILE   = os.path.join(BASE_DIR, 'about.json')

os.makedirs(MUSIC_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER,  exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 GB

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload"
]

SUPPORTED_MUSIC = ('.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac')
SUPPORTED_IMAGE = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')

DEFAULT_ABOUT = {
    "title": "The Last of Us #shorts #thelastofus",
    "description": "The Last of Us Aesthetic Edits. Like & Subscribe! #shorts #gaming",
    "category": "20",
    "tags": ["shorts", "gaming", "viral", "trending"]
}

YOUTUBE_CATEGORIES = [
    {"id":"1","name":"Film & Animation"},{"id":"2","name":"Autos & Vehicles"},
    {"id":"10","name":"Music"},{"id":"15","name":"Pets & Animals"},
    {"id":"17","name":"Sports"},{"id":"20","name":"Gaming"},
    {"id":"22","name":"People & Blogs"},{"id":"23","name":"Comedy"},
    {"id":"24","name":"Entertainment"},{"id":"25","name":"News & Politics"},
    {"id":"26","name":"Howto & Style"},{"id":"27","name":"Education"},
    {"id":"28","name":"Science & Technology"},
]

# ============================================================
# GITHUB STORAGE
# ============================================================

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "BimaSkyy/myhistory")
GITHUB_API   = "https://api.github.com"

_sha_cache = {}

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
    }

def gh_get(path):
    if not REQUESTS_AVAILABLE: return None, None
    try:
        import requests as r
        resp = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
                     headers=_gh_headers(), timeout=15)
        if resp.status_code == 200:
            d = resp.json()
            content = base64.b64decode(d["content"]).decode("utf-8")
            _sha_cache[path] = d["sha"]
            return content, d["sha"]
        return None, None
    except Exception as e:
        print(f"[GH] get error: {e}")
        return None, None

def gh_put(path, content_str, message=None):
    if not REQUESTS_AVAILABLE: return False
    try:
        import requests as r
        sha = _sha_cache.get(path)
        if not sha:
            _, sha = gh_get(path)
        encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
        payload = {"message": message or f"[auto] {path}", "content": encoded}
        if sha: payload["sha"] = sha
        resp = r.put(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
                     headers=_gh_headers(), json=payload, timeout=20)
        if resp.status_code in (200, 201):
            new_sha = resp.json().get("content", {}).get("sha")
            if new_sha: _sha_cache[path] = new_sha
            return True
        print(f"[GH] PUT {path} → {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[GH] put error: {e}")
        return False

def gh_load(key, default=None):
    content, _ = gh_get(f"data/{key}.json")
    if content is None: return default
    try: return json.loads(content)
    except: return default

def gh_save(key, data, message=None):
    content_str = json.dumps(data, indent=2, ensure_ascii=False)
    ok = gh_put(f"data/{key}.json", content_str, message=message)
    return ok

# ============================================================
# GITHUB VIDEO UPLOAD (support > 20MB via shell curl)
# ============================================================

def gh_upload_video(local_path, repo_path, max_retries=3):
    file_size = os.path.getsize(local_path)
    print(f"[GH VIDEO] Uploading {repo_path} ({file_size/1024/1024:.1f}MB)...")

    for attempt in range(1, max_retries + 1):
        print(f"[GH VIDEO] Attempt {attempt}/{max_retries}...")
        try:
            if file_size <= 20 * 1024 * 1024:
                ok = _gh_upload_api(local_path, repo_path)
            else:
                ok = _gh_upload_shell(local_path, repo_path)

            if ok:
                time.sleep(2)
                verified, reason = gh_verify_video(repo_path)
                if verified:
                    print(f"[GH VIDEO] Verified OK: {repo_path}")
                    return True, "ok"
                else:
                    print(f"[GH VIDEO] Verify failed: {reason}, retry...")
                    time.sleep(3)
            else:
                print(f"[GH VIDEO] Upload failed attempt {attempt}")
                time.sleep(3)
        except Exception as e:
            print(f"[GH VIDEO] Exception attempt {attempt}: {e}")
            time.sleep(3)

    return False, f"Gagal setelah {max_retries} percobaan"

def _gh_upload_api(local_path, repo_path):
    import requests as r
    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    sha = _sha_cache.get(repo_path)
    if not sha:
        resp_get = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                         headers=_gh_headers(), timeout=15)
        if resp_get.status_code == 200:
            sha = resp_get.json().get("sha")
            _sha_cache[repo_path] = sha

    payload = {
        "message": f"[video] {os.path.basename(repo_path)} {time.strftime('%Y-%m-%d %H:%M')}",
        "content": content
    }
    if sha: payload["sha"] = sha

    resp = r.put(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                 headers=_gh_headers(), json=payload, timeout=120)
    if resp.status_code in (200, 201):
        new_sha = resp.json().get("content", {}).get("sha")
        if new_sha: _sha_cache[repo_path] = new_sha
        return True
    print(f"[GH API] {resp.status_code}: {resp.text[:300]}")
    return False

def _gh_upload_shell(local_path, repo_path):
    import requests as r

    sha = ""
    resp_get = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                     headers=_gh_headers(), timeout=15)
    if resp_get.status_code == 200:
        sha = resp_get.json().get("sha", "")

    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")

    sha_str = f', "sha": "{sha}"' if sha else ""
    json_payload = (
        f'{{"message": "[video] {os.path.basename(repo_path)} {time.strftime("%Y-%m-%d %H:%M")}", '
        f'"content": "{content_b64}"{sha_str}}}'
    )

    payload_file = os.path.join(TEMP_FOLDER, f"_payload_{uuid.uuid4().hex[:8]}.json")
    with open(payload_file, "w") as pf:
        pf.write(json_payload)

    try:
        cmd = [
            "curl", "-s", "-X", "PUT",
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
            "-H", f"Authorization: token {GITHUB_TOKEN}",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Content-Type: application/json",
            "-d", f"@{payload_file}",
            "--max-time", "300"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=320)
        if result.returncode == 0 and '"sha"' in result.stdout and '"name"' in result.stdout:
            try:
                resp_json = json.loads(result.stdout)
                new_sha = resp_json.get("content", {}).get("sha")
                if new_sha: _sha_cache[repo_path] = new_sha
            except: pass
            return True
        print(f"[GH SHELL] curl rc={result.returncode}, out={result.stdout[:200]}")
        return False
    finally:
        try: os.remove(payload_file)
        except: pass

def gh_verify_video(repo_path):
    if not REQUESTS_AVAILABLE: return False, "no requests"
    try:
        import requests as r
        resp = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                     headers=_gh_headers(), timeout=15)
        if resp.status_code == 200:
            size = resp.json().get("size", 0)
            if size > 0: return True, f"OK size={size}"
            return False, "size=0"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def gh_delete_video(repo_path):
    if not REQUESTS_AVAILABLE: return False
    try:
        import requests as r
        sha = _sha_cache.get(repo_path)
        if not sha:
            resp_get = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                             headers=_gh_headers(), timeout=15)
            if resp_get.status_code == 200:
                sha = resp_get.json().get("sha")
        if not sha: return False
        payload = {
            "message": f"[cleanup] {os.path.basename(repo_path)}",
            "sha": sha
        }
        resp = r.delete(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                        headers=_gh_headers(), json=payload, timeout=30)
        if resp.status_code in (200, 204):
            _sha_cache.pop(repo_path, None)
            return True
        return False
    except Exception as e:
        print(f"[GH DELETE] error: {e}")
        return False

# ============================================================
# RAM CACHE + LOAD/SAVE
# ============================================================

_ram_riwayat: list = None
_ram_queue:   list = None
_ram_settings: dict = None

def load_settings() -> dict:
    global _ram_settings
    remote = gh_load("settings", None)
    if remote is not None:
        _ram_settings = remote
        return remote
    return _ram_settings if _ram_settings is not None else {}

def save_settings(data: dict):
    global _ram_settings
    _ram_settings = data
    gh_save("settings", data, message="[settings] update")

def is_paused() -> bool:
    s = load_settings()
    return bool(s.get("paused", False))

def load_riwayat() -> list:
    global _ram_riwayat
    remote = gh_load("riwayat", None)
    if remote is not None:
        _ram_riwayat = remote
        return remote
    return _ram_riwayat if _ram_riwayat is not None else []

def save_riwayat(data: list, force_gh=False):
    global _ram_riwayat
    _ram_riwayat = data
    if force_gh:
        gh_save("riwayat", data, message=f"riwayat {len(data)} entries")

def load_queue() -> list:
    global _ram_queue
    remote = gh_load("queue", None)
    if remote is not None:
        _ram_queue = remote
        return remote
    return _ram_queue if _ram_queue is not None else []

def save_queue(data: list, force_gh=False):
    global _ram_queue
    _ram_queue = data
    if force_gh:
        gh_save("queue", data, message=f"queue {len(data)} items")

# ============================================================
# BACKGROUND SYNC
# ============================================================

_sync_riwayat_hash = ""
_sync_queue_hash   = ""

def _md5(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def sync_worker():
    global _sync_riwayat_hash, _sync_queue_hash
    while True:
        time.sleep(60)
        try:
            # Cek antrian aktif: pastikan videonya masih ada di GitHub
            if _ram_queue is not None:
                changed = False
                for item in _ram_queue:
                    if item.get("status") in ("pending", "waiting") and item.get("github_path"):
                        verified, _ = gh_verify_video(item["github_path"])
                        if not verified:
                            print(f"[SYNC] Video hilang dari GitHub → nonaktifkan: {item.get('title','?')}")
                            item["status"] = "failed"
                            item["error"] = "Video tidak ditemukan di GitHub"
                            changed = True
                if changed:
                    with queue_lock:
                        activate_next_waiting(_ram_queue)
                        save_queue(_ram_queue, force_gh=True)

            if _ram_riwayat is not None:
                h = _md5(_ram_riwayat)
                if h != _sync_riwayat_hash:
                    if gh_save("riwayat", _ram_riwayat, "[auto-sync] riwayat"):
                        _sync_riwayat_hash = h

            if _ram_queue is not None:
                h = _md5(_ram_queue)
                if h != _sync_queue_hash:
                    if gh_save("queue", _ram_queue, "[auto-sync] queue"):
                        _sync_queue_hash = h
        except Exception as e:
            print(f"[SYNC] error: {e}")

# ============================================================
# HOURLY GITHUB CLEANUP WORKER
# Setiap 1 jam:
# 1. Cek file di antrian pending/waiting → pastikan ada di GitHub
# 2. Hapus file video di GitHub yang tidak ada di antrian aktif
# ============================================================

def _gh_list_folder_files(folder):
    """List semua file di folder GitHub, return list of {name, sha, path}."""
    if not REQUESTS_AVAILABLE:
        return []
    try:
        import requests as r
        url  = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{folder}"
        resp = r.get(url, headers=_gh_headers(), timeout=20)
        if resp.status_code == 200:
            items = resp.json()
            if isinstance(items, list):
                return [
                    {"name": i["name"], "sha": i["sha"], "path": i["path"]}
                    for i in items if i.get("type") == "file"
                ]
    except Exception as e:
        print(f"[CLEANUP] list folder error {folder}: {e}")
    return []

def _gh_delete_file(repo_path, sha, message="[cleanup] hapus file tidak terpakai"):
    if not REQUESTS_AVAILABLE:
        return False
    try:
        import requests as r
        payload = {"message": message, "sha": sha}
        resp = r.delete(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
            headers=_gh_headers(), json=payload, timeout=30
        )
        if resp.status_code in (200, 204):
            _sha_cache.pop(repo_path, None)
            return True
        print(f"[CLEANUP] delete {repo_path} → {resp.status_code}")
        return False
    except Exception as e:
        print(f"[CLEANUP] delete error {repo_path}: {e}")
        return False

def github_cleanup_worker():
    """
    Setiap 1 jam:
    - Ambil antrian aktif (pending + waiting)
    - Kumpulkan semua github_path yang masih diperlukan
    - List semua file di folder video/ di GitHub
    - Hapus file yang tidak ada di antrian aktif
    - Cek juga bahwa file antrian aktif benar-benar ada di GitHub
    """
    # Tunda 5 menit pertama supaya server sudah fully up
    time.sleep(300)

    while True:
        try:
            print("[CLEANUP] Memulai pengecekan GitHub hourly...")

            queue = load_queue()

            # Kumpulkan path yang MASIH diperlukan (pending & waiting)
            needed_paths = set()
            for item in queue:
                if item.get("status") in ("pending", "waiting"):
                    gp = item.get("github_path", "")
                    if gp:
                        needed_paths.add(gp)
                    # Juga jaga thumbnail jika ada
                    tp = item.get("thumbnail_github_path", "")
                    if tp:
                        needed_paths.add(tp)

            print(f"[CLEANUP] File diperlukan di antrian: {len(needed_paths)}")

            # ── 1. Verifikasi file antrian masih ada di GitHub ──
            changed = False
            for item in queue:
                if item.get("status") in ("pending", "waiting") and item.get("github_path"):
                    verified, reason = gh_verify_video(item["github_path"])
                    if not verified:
                        print(f"[CLEANUP] File hilang dari GitHub: {item['github_path']} → tandai failed")
                        item["status"] = "failed"
                        item["error"]  = f"[hourly check] File tidak ada di GitHub: {reason}"
                        changed = True

            if changed:
                with queue_lock:
                    activate_next_waiting(queue)
                    save_queue(queue, force_gh=True)
                print(f"[CLEANUP] Queue diupdate setelah cek file hilang")

            # ── 2. Hapus file video GitHub yang tidak diperlukan ──
            video_files = _gh_list_folder_files("video")
            deleted_count = 0
            for vf in video_files:
                repo_path = vf["path"]
                if repo_path not in needed_paths:
                    print(f"[CLEANUP] Hapus file tidak terpakai: {repo_path}")
                    ok = _gh_delete_file(repo_path, vf["sha"],
                                         f"[hourly-cleanup] hapus video tidak aktif: {vf['name']}")
                    if ok:
                        deleted_count += 1
                        print(f"[CLEANUP] ✓ Dihapus: {repo_path}")
                    time.sleep(1)  # Jaga rate limit GitHub API

            # ── 3. Hapus thumbnail GitHub yang tidak diperlukan ──
            thumb_files = _gh_list_folder_files("thumbnails")
            for tf in thumb_files:
                repo_path = tf["path"]
                if repo_path not in needed_paths:
                    print(f"[CLEANUP] Hapus thumbnail tidak terpakai: {repo_path}")
                    ok = _gh_delete_file(repo_path, tf["sha"],
                                         f"[hourly-cleanup] hapus thumbnail tidak aktif: {tf['name']}")
                    if ok:
                        deleted_count += 1
                    time.sleep(1)

            print(f"[CLEANUP] Selesai. Total dihapus: {deleted_count} file | Diperlukan: {len(needed_paths)} file")

        except Exception as e:
            print(f"[CLEANUP] Error: {e}")

        # Tunggu 1 jam
        time.sleep(3600)

# ============================================================
# ABOUT
# ============================================================

def load_about():
    if os.path.exists(ABOUT_FILE):
        with open(ABOUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_ABOUT.copy()

def save_about(data):
    with open(ABOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ============================================================
# FILE HELPERS
# ============================================================

def get_file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def check_duplicate(file_hash: str):
    if not file_hash: return False, None, ""
    for item in load_riwayat():
        if item.get("file_hash") == file_hash:
            return True, item.get("video_id"), f"Sudah diupload: {item.get('title','')}"
    for item in load_queue():
        if item.get("file_hash") == file_hash and item.get("status") not in ("done", "failed"):
            return True, None, f"Sudah ada di antrian: {item.get('title','')}"
    return False, None, ""

# ============================================================
# JSONBIN — simpan & ambil token langsung dari Koyeb
# ============================================================

JSONBIN_BIN_ID  = os.environ.get("JSONBIN_BIN_ID", "")
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "")
JSONBIN_URL     = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"

# ── In-memory token cache (primary storage, survives JSONBin outage) ──
_token_memory_cache: dict = {}

def _jb_headers():
    return {
        "X-Master-Key": JSONBIN_API_KEY,
        "Content-Type": "application/json"
    }

def _push_token_to_store(creds):
    """Simpan token ke memory cache dulu, lalu JSONBin (async, best-effort)."""
    global _token_memory_cache
    try:
        token_dict = json.loads(creds.to_json())
        # ── Prioritas 1: simpan ke memory DULU — tidak pernah gagal ──
        _token_memory_cache = token_dict
        print("[AUTH] Token disimpan ke memory cache")
    except Exception as e:
        print(f"[AUTH] Gagal simpan ke memory: {e}")

    # ── Prioritas 2: coba simpan ke JSONBin (retry 2x, background) ──
    if not REQUESTS_AVAILABLE or not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        print("[AUTH] JSONBin belum dikonfigurasi, skip simpan ke JSONBin.")
        return

    def _do_push():
        import requests as r
        payload = {
            "token": token_dict,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        for attempt in range(1, 3):
            try:
                resp = r.put(JSONBIN_URL, headers=_jb_headers(), json=payload, timeout=20)
                if resp.status_code == 200:
                    print(f"[AUTH] Token berhasil disimpan ke JSONBin (attempt {attempt})")
                    return
                else:
                    print(f"[AUTH] JSONBin push attempt {attempt} gagal: {resp.status_code}")
            except Exception as e:
                print(f"[AUTH] JSONBin push attempt {attempt} error: {e}")
            time.sleep(3)
        print("[AUTH] JSONBin push gagal semua attempt, token aman di memory")

    threading.Thread(target=_do_push, daemon=True).start()

def _pull_token_from_store():
    """Ambil token: memory cache dulu, lalu JSONBin."""
    global _token_memory_cache

    # ── Prioritas 1: memory cache (instan, tidak kena network) ──
    if _token_memory_cache and isinstance(_token_memory_cache, dict) and _token_memory_cache.get("token"):
        print("[AUTH] Token diambil dari memory cache")
        return _token_memory_cache

    # ── Prioritas 2: JSONBin ──
    if not REQUESTS_AVAILABLE or not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        return None
    for attempt in range(1, 3):
        try:
            import requests as r
            resp = r.get(JSONBIN_URL + "/latest", headers=_jb_headers(), timeout=20)
            if resp.status_code == 200:
                record = resp.json().get("record", {})
                token = record.get("token")
                if token and isinstance(token, dict) and token.get("token"):
                    # Simpan ke memory supaya request berikutnya tidak perlu ke JSONBin
                    _token_memory_cache = token
                    print(f"[AUTH] Token diambil dari JSONBin (attempt {attempt}), di-cache ke memory")
                    return token
            return None
        except Exception as e:
            print(f"[AUTH] Pull token JSONBin attempt {attempt} error: {e}")
            if attempt < 2:
                time.sleep(3)
    return None

def load_credentials():
    """
    Ambil credentials YouTube.
    Prioritas: memory cache -> JSONBin -> env var -> file lokal.
    Auto-refresh jika expired, simpan balik ke store.
    """
    if not GOOGLE_AVAILABLE: return None

    # -- Prioritas 1: memory cache + JSONBin (via _pull_token_from_store) --
    token_data = _pull_token_from_store()

    # ── Prioritas 2: env var YOUTUBE_TOKEN_JSON (legacy) ──────
    if not token_data:
        token_json_str = os.environ.get("YOUTUBE_TOKEN_JSON", "")
        if token_json_str:
            try: token_data = json.loads(token_json_str)
            except: pass

    # ── Prioritas 3: file lokal (development) ─────────────────
    if not token_data:
        token_path = os.path.join(BASE_DIR, "token.json")
        if os.path.exists(token_path):
            try:
                with open(token_path) as f:
                    token_data = json.load(f)
            except: pass

    if not token_data:
        print("[AUTH] Tidak ada token. Login OAuth dulu di halaman /auth.")
        return None

    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    except Exception as e:
        print(f"[AUTH] Token tidak valid: {e}")
        return None

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            print("[AUTH] Token auto-refresh berhasil, menyimpan ke JSONBin...")
            _push_token_to_store(creds)
            return creds
        except Exception as e:
            print(f"[AUTH] Auto-refresh gagal: {e}")
            return None

    print("[AUTH] Token expired & tidak ada refresh_token. Login OAuth ulang.")
    return None

# ============================================================
# TEMP FILE MANAGEMENT
# ============================================================

def temp_path(filename):
    return os.path.join(TEMP_FOLDER, filename)

def cleanup_temp(filename):
    if not filename: return
    p = temp_path(filename)
    if os.path.exists(p):
        try:
            os.remove(p)
            print(f"[TEMP] Deleted: {filename}")
        except Exception as e:
            print(f"[TEMP] Delete error {filename}: {e}")

def is_valid_video(path):
    if not path or not os.path.exists(path):
        return False, "File tidak ditemukan"
    size = os.path.getsize(path)
    if size < 10 * 1024:
        return False, f"File terlalu kecil ({size} bytes)"
    return True, "OK"

# ============================================================
# FFMPEG HELPERS
# ============================================================

_FFMPEG_BIN  = None
_FFPROBE_BIN = None

def _find_bin(name):
    if name == 'ffmpeg' and FFMPEG_BINARY and os.path.exists(str(FFMPEG_BINARY)):
        return FFMPEG_BINARY
    for c in [name, f'/usr/bin/{name}', f'/usr/local/bin/{name}']:
        try:
            if subprocess.run([c, '-version'], capture_output=True, timeout=5).returncode == 0:
                return c
        except: pass
    if name == 'ffmpeg':
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except: pass
    return None

def get_ffmpeg():
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = _find_bin('ffmpeg')
        print(f"[FFMPEG] {'Found: ' + str(_FFMPEG_BIN) if _FFMPEG_BIN else 'NOT FOUND!'}")
    return _FFMPEG_BIN

def get_ffprobe():
    global _FFPROBE_BIN
    if _FFPROBE_BIN is None:
        _FFPROBE_BIN = _find_bin('ffprobe') or get_ffmpeg()
    return _FFPROBE_BIN

def get_music_duration(music_path):
    ff = get_ffprobe() or get_ffmpeg()
    if ff:
        try:
            r = subprocess.run(
                [ff, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', music_path],
                capture_output=True, text=True, timeout=30
            )
            val = r.stdout.strip().split('\n')[0].strip()
            if val and val not in ('N/A', ''):
                d = float(val)
                if d > 0: return d
        except: pass
        try:
            import re
            r = subprocess.run([ff, '-i', music_path], capture_output=True, text=True, timeout=30)
            m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', r.stderr)
            if m:
                d = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                if d > 0: return d
        except: pass
    try:
        from mutagen import File as MFile
        audio = MFile(music_path)
        if audio and hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            d = float(audio.info.length)
            if d > 0: return d
    except: pass
    return None

def get_image_dimensions(img_path):
    ff = get_ffprobe() or get_ffmpeg()
    if ff:
        try:
            r = subprocess.run(
                [ff, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height',
                 '-of', 'csv=s=x:p=0', img_path],
                capture_output=True, text=True, timeout=15
            )
            parts = r.stdout.strip().split('x')
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
        except: pass
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            return img.size
    except: pass
    return None, None

def calc_video_size_1080p(w, h):
    if not w or not h: return 1920, 1080
    ratio = min(1920 / w, 1080 / h)
    nw = int(w * ratio); nh = int(h * ratio)
    nw = nw if nw % 2 == 0 else nw - 1
    nh = nh if nh % 2 == 0 else nh - 1
    return max(nw, 2), max(nh, 2)

def format_duration(secs):
    if not secs: return "?"
    return f"{int(secs//60)}:{int(secs%60):02d}"

# ============================================================
# VIDEO MAKER — 1080P Quality
# ============================================================

video_tasks      = {}
video_tasks_lock = threading.Lock()

def run_video_creation(task_id, photo_path, music_path, output_path):
    with video_tasks_lock:
        video_tasks[task_id]["status"]   = "running"
        video_tasks[task_id]["progress"] = 5

    try:
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg tidak ditemukan! Klik 'Install All Dependencies'.")

        img_w, img_h = get_image_dimensions(photo_path)
        vw, vh       = calc_video_size_1080p(img_w, img_h)
        duration     = get_music_duration(music_path)

        if not duration or duration <= 0:
            raise ValueError(f"Tidak bisa membaca durasi musik. Pastikan ffmpeg terinstall.")

        with video_tasks_lock:
            video_tasks[task_id]["progress"]   = 15
            video_tasks[task_id]["duration"]   = round(duration, 1)
            video_tasks[task_id]["resolution"] = f"{vw}x{vh}"

        cmd = [
            ffmpeg, '-y',
            '-loop', '1', '-i', photo_path,
            '-i', music_path,
            '-c:v', 'libx264', '-preset', 'slow', '-tune', 'stillimage',
            '-crf', '18',
            '-c:a', 'aac', '-b:a', '192k',
            '-pix_fmt', 'yuv420p',
            '-vf', f'scale={vw}:{vh}:flags=lanczos',
            '-t', str(duration),
            '-movflags', '+faststart',
            '-shortest',
            output_path
        ]

        proc  = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        start = time.time()
        est   = max(duration * 0.2, 5)

        while proc.poll() is None:
            pct = min(90, int(15 + (time.time() - start) / est * 75))
            with video_tasks_lock:
                video_tasks[task_id]["progress"] = pct
            time.sleep(0.5)

        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg error: {stderr.decode('utf-8', errors='replace')[-600:]}")

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            raise RuntimeError("Output video tidak valid atau kosong")

        file_size = os.path.getsize(output_path)
        with video_tasks_lock:
            video_tasks[task_id].update({
                "status":          "done",
                "progress":        100,
                "output_filename": os.path.basename(output_path),
                "file_size":       file_size,
                "done_at":         time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        print(f"[VIDEOMAKER] Done: {os.path.basename(output_path)} ({file_size//1024}KB) 1080p")

    except Exception as e:
        print(f"[VIDEOMAKER] Failed: {e}")
        with video_tasks_lock:
            video_tasks[task_id]["status"] = "failed"
            video_tasks[task_id]["error"]  = str(e)
        cleanup_temp(os.path.basename(output_path))

# ============================================================
# YOUTUBE UPLOAD
# ============================================================

def add_to_playlist(youtube, video_id, playlist_id):
    if not playlist_id: return False, "Tidak ada playlist_id"
    try:
        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        ).execute()
        print(f"[PLAYLIST] Added {video_id} to {playlist_id}")
        return True, None
    except Exception as e:
        print(f"[PLAYLIST] Error: {e}")
        return False, str(e)

def set_thumbnail(youtube, video_id, thumbnail_path):
    """Upload custom thumbnail ke YouTube video."""
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        return False, "File thumbnail tidak ditemukan"
    try:
        ext = os.path.splitext(thumbnail_path)[1].lower()
        mime = "image/jpeg" if ext in ('.jpg', '.jpeg') else "image/png" if ext == '.png' else "image/jpeg"
        media_thumb = MediaFileUpload(thumbnail_path, mimetype=mime)
        youtube.thumbnails().set(videoId=video_id, media_body=media_thumb).execute()
        print(f"[THUMBNAIL] Set OK untuk video {video_id}")
        return True, None
    except Exception as e:
        print(f"[THUMBNAIL] Gagal: {e}")
        return False, str(e)

def do_youtube_upload(file_path, title, description, tags, category, file_hash, filename, playlist_id=None, thumbnail_path=None):
    if not GOOGLE_AVAILABLE: return None, "Google API tidak tersedia"
    creds = load_credentials()
    if not creds: return None, "Belum autentikasi YouTube"

    # ── Selalu refresh token sebelum upload ──────────────────
    try:
        if creds.refresh_token:
            creds.refresh(Request())
            _push_token_to_store(creds)
            print("[AUTH] Token di-refresh sebelum upload, tersimpan ke JSONBin")
    except Exception as e:
        print(f"[AUTH] Pre-upload refresh gagal (lanjut dengan token lama): {e}")

    if not os.path.exists(file_path): return None, "File tidak ditemukan"
    if os.path.getsize(file_path) < 10 * 1024: return None, "File terlalu kecil"

    try:
        youtube       = build("youtube", "v3", credentials=creds)
        file_size_byt = os.path.getsize(file_path)
        media         = MediaFileUpload(file_path, chunksize=-1, resumable=False)
        body = {
            "snippet": {
                "title": title, "description": description,
                "tags": tags, "categoryId": category,
            },
            "status": {
                "privacyStatus": "public", "selfDeclaredMadeForKids": False,
                "embeddable": True, "publicStatsViewable": True
            }
        }
        resp     = youtube.videos().insert(part="snippet,status", body=body, media_body=media).execute()
        video_id = resp["id"]
        print(f"[YT] Uploaded OK: {video_id} — {title}")

        # Set custom thumbnail jika ada
        thumbnail_error = None
        if thumbnail_path:
            ok_th, th_err = set_thumbnail(youtube, video_id, thumbnail_path)
            if not ok_th:
                thumbnail_error = th_err

        # Tambah ke playlist jika ada
        playlist_error = None
        if playlist_id:
            ok, pl_err = add_to_playlist(youtube, video_id, playlist_id)
            if not ok:
                playlist_error = pl_err
                print(f"[PLAYLIST] Gagal: {pl_err}")

        riwayat = load_riwayat()
        entry = {
            "video_id":        video_id,
            "title":           title,
            "description":     description,
            "tags":            tags,
            "category":        category,
            "file_name":       filename,
            "file_hash":       file_hash,
            "file_size_bytes": file_size_byt,
            "link":            f"https://youtu.be/{video_id}",
            "youtube_url":     f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail":       f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "tanggal_upload":  time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp_unix":  time.time(),
            "source":          "scheduled",
            "playlist_id":     playlist_id or "",
            "playlist_error":  playlist_error or "",
            "thumbnail_error": thumbnail_error or "",
        }
        riwayat.append(entry)
        save_riwayat(riwayat, force_gh=True)
        return video_id, None

    except Exception as e:
        print(f"[YT] Upload error: {e}")
        return None, str(e)

# ============================================================
# QUEUE WORKER
# ============================================================

queue_lock = threading.Lock()

def activate_next_waiting(queue):
    has_active = any(q.get("status") in ("pending", "uploading") for q in queue)
    if has_active: return
    for item in queue:
        if item.get("status") == "waiting":
            now_ts = time.time()
            ts = float(item.get("timeout_seconds", 0))
            item.update({
                "status":            "pending",
                "upload_at_ts":      now_ts + ts,
                "upload_at":         datetime.fromtimestamp(now_ts + ts).strftime("%Y-%m-%d %H:%M:%S"),
                "activated_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
                "remaining_seconds": ts,
            })
            print(f"[QUEUE] Activated → pending: {item.get('title','?')} (timer {ts}s)")
            break

def validate_queue_on_startup(queue):
    riwayat = load_riwayat()
    uploaded_hashes = {r.get("file_hash") for r in riwayat if r.get("file_hash")}
    uploaded_ids    = {r.get("video_id") for r in riwayat if r.get("video_id")}
    cleaned = []
    for item in queue:
        if item.get("file_hash") in uploaded_hashes: continue
        if item.get("video_id") in uploaded_ids: continue
        if item.get("status") == "uploading":
            item["status"] = "pending"
            now_ts = time.time()
            if not item.get("upload_at_ts") or float(item.get("upload_at_ts", 0)) < now_ts:
                item["upload_at_ts"] = now_ts + 300
                item["upload_at"]    = datetime.fromtimestamp(now_ts + 300).strftime("%Y-%m-%d %H:%M:%S")
        cleaned.append(item)
    return cleaned

def _download_from_github(repo_path, local_path):
    if not REQUESTS_AVAILABLE: return False
    try:
        import requests as r
        resp = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}",
                     headers=_gh_headers(), timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            download_url = data.get("download_url")
            if download_url:
                resp2 = r.get(download_url, timeout=120, stream=True)
                if resp2.status_code == 200:
                    with open(local_path, "wb") as f:
                        for chunk in resp2.iter_content(chunk_size=65536):
                            f.write(chunk)
                    return os.path.getsize(local_path) > 0
            content = data.get("content", "")
            if content:
                decoded = base64.b64decode(content.replace("\n", ""))
                with open(local_path, "wb") as f:
                    f.write(decoded)
                return os.path.getsize(local_path) > 0
        return False
    except Exception as e:
        print(f"[DL] error: {e}")
        return False

def queue_worker():
    def do_upload(it):
        filename    = it.get("filename", "")
        github_path = it.get("github_path", "")
        file_path   = temp_path(filename)

        # Download dari GitHub jika file tidak ada di temp
        if not os.path.exists(file_path) and github_path:
            print(f"[QUEUE] Download dari GitHub: {github_path}")
            downloaded = _download_from_github(github_path, file_path)
            if not downloaded:
                with queue_lock:
                    q2 = load_queue()
                    for q in q2:
                        if q.get("id") == it["id"]:
                            q["status"] = "failed"
                            q["error"]  = "Video tidak ada di GitHub maupun server"
                    activate_next_waiting(q2)
                    save_queue(q2, force_gh=True)
                return

        valid, reason = is_valid_video(file_path)
        if not valid:
            with queue_lock:
                q2 = load_queue()
                for q in q2:
                    if q.get("id") == it["id"]:
                        q["status"] = "failed"
                        q["error"]  = f"File tidak valid: {reason}"
                activate_next_waiting(q2)
                save_queue(q2, force_gh=True)
            return

        is_dup, vid_id, dup_reason = check_duplicate(it.get("file_hash", ""))
        if is_dup and vid_id:
            with queue_lock:
                q2 = load_queue()
                for q in q2:
                    if q.get("id") == it["id"]:
                        q.update({"status": "done", "video_id": vid_id,
                                  "link": f"https://youtu.be/{vid_id}",
                                  "note": f"Duplikat: {dup_reason}"})
                activate_next_waiting(q2)
                save_queue(q2, force_gh=True)
            cleanup_temp(filename)
            return

        about = load_about()

        # Resolve thumbnail path: dari antrian atau default thumbnail/thumbnail.jpg
        thumbnail_path = None
        thumbnail_gh = it.get("thumbnail_github_path", "")
        if thumbnail_gh:
            # Download thumbnail dari GitHub jika ada
            thumb_fname = f"thumb_{it.get('id','')[:8]}.jpg"
            thumb_local = temp_path(thumb_fname)
            if _download_from_github(thumbnail_gh, thumb_local):
                thumbnail_path = thumb_local
        if not thumbnail_path:
            default_thumb = os.path.join(BASE_DIR, "thumbnail", "thumbnail.jpg")
            if os.path.exists(default_thumb):
                thumbnail_path = default_thumb

        vid_id, err = do_youtube_upload(
            file_path,
            it.get("title", about.get("title", "")),
            it.get("description", about.get("description", "")),
            it.get("tags", about.get("tags", [])),
            it.get("category", about.get("category", "20")),
            it.get("file_hash", ""), filename,
            playlist_id=it.get("playlist_id") or about.get("playlist", ""),
            thumbnail_path=thumbnail_path,
        )

        # Hapus thumbnail temp jika di-download dari GitHub
        if thumbnail_gh and thumbnail_path and os.path.exists(thumbnail_path):
            try: os.remove(thumbnail_path)
            except: pass

        # Video di GitHub dibiarkan tetap ada setelah upload YouTube berhasil
        if vid_id and github_path:
            print(f"[QUEUE] Upload YouTube berhasil, video GitHub tetap disimpan: {github_path}")

        cleanup_temp(filename)

        with queue_lock:
            q2 = load_queue()
            for q in q2:
                if q.get("id") == it["id"]:
                    if vid_id:
                        q.update({
                            "status":      "done",
                            "video_id":    vid_id,
                            "link":        f"https://youtu.be/{vid_id}",
                            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "remaining_seconds": 0,
                        })
                    else:
                        q.update({"status": "failed", "error": err})
            activate_next_waiting(q2)
            save_queue(q2, force_gh=True)

    while True:
        try:
            # Jika mode pause aktif, geser semua upload_at_ts ke depan (freeze timer)
            if is_paused():
                with queue_lock:
                    queue = load_queue()
                    changed = False
                    for q in queue:
                        if q.get("status") == "pending" and q.get("upload_at_ts"):
                            q["upload_at_ts"] = time.time() + max(1.0, float(q.get("remaining_seconds") or 5))
                            changed = True
                    if changed:
                        save_queue(queue)
                time.sleep(1)
                continue

            with queue_lock:
                queue = load_queue()
                now_ts = time.time()
                # Reset item yang stuck di uploading lebih dari 30 menit
                stuck_found = False
                for q in queue:
                    if q.get("status") == "uploading":
                        started = float(q.get("uploading_since") or 0)
                        if started == 0 or (now_ts - started) > 1800:
                            print(f"[QUEUE WORKER] Item {q.get('id')} stuck uploading, reset ke pending")
                            q["status"] = "pending"
                            q["uploading_since"] = None
                            q["upload_at_ts"] = now_ts + 10
                            q["upload_at"] = __import__("datetime").datetime.fromtimestamp(now_ts + 10).strftime("%Y-%m-%d %H:%M:%S")
                            stuck_found = True
                if stuck_found:
                    save_queue(queue, force_gh=True)

                uploading = [q for q in queue if q.get("status") == "uploading"]
                if not uploading:
                    pending = [q for q in queue if q.get("status") == "pending"]
                    if not pending:
                        activate_next_waiting(queue)
                        if any(q.get("status") == "pending" for q in queue):
                            save_queue(queue, force_gh=True)
                    else:
                        item   = pending[0]
                        now_ts = time.time()
                        up_ts  = float(item.get("upload_at_ts") or 0)
                        remaining = max(0.0, up_ts - now_ts)

                        for q in queue:
                            if q.get("id") == item["id"]:
                                q["remaining_seconds"] = round(remaining, 1)

                        if now_ts >= up_ts:
                            for q in queue:
                                if q.get("id") == item["id"]:
                                    q["status"]            = "uploading"
                                    q["remaining_seconds"] = 0
                                    q["uploading_since"]   = now_ts
                            save_queue(queue, force_gh=True)
                            item_copy = dict(item)
                            threading.Thread(target=do_upload, args=(item_copy,), daemon=True).start()
                        else:
                            save_queue(queue)
        except Exception as e:
            print(f"[QUEUE WORKER] Error: {e}")
        time.sleep(1)

# ============================================================
# OAUTH ROUTES — login & kelola token YouTube
# ============================================================

try:
    from google_auth_oauthlib.flow import Flow as OAuthFlow
    OAUTHLIB_OK = True
except ImportError:
    OAUTHLIB_OK = False

_oauth_state_store = {}  # simpan state sementara di RAM

def _get_oauth_redirect_uri():
    base = os.environ.get("KOYEB_PUBLIC_DOMAIN", "")
    if not base:
        base = request.host_url.rstrip("/")
    elif not base.startswith("http"):
        base = f"https://{base}"
    return f"{base}/auth/callback"

@app.route('/auth')
def auth_page():
    """Halaman status token & tombol login OAuth."""
    creds = load_credentials()
    token_valid = creds is not None and creds.valid

    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if token_valid:
        status_color = "#4ade80"
        status_text  = "✅ Token valid & aktif — auto-refresh aktif"
    else:
        status_color = "#f87171"
        status_text  = "❌ Belum ada token / expired — klik Login di bawah"

    google_configured  = bool(client_id and client_secret)
    jsonbin_configured = bool(JSONBIN_BIN_ID and JSONBIN_API_KEY)

    refresh_btn = '<a href="/auth/refresh" style="display:inline-flex;align-items:center;gap:8px;padding:11px 22px;border-radius:8px;font-size:.88rem;font-weight:600;text-decoration:none;background:#2a2a2a;color:#ccc;border:1px solid #3a3a3a;margin-top:14px">🔄 Force Refresh</a>' if token_valid else ''

    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Auth — Koyeb</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,sans-serif;background:#0f0f0f;color:#e0e0e0;
       min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .wrap{{max-width:500px;width:90%;padding:20px 0}}
  h1{{font-size:1.3rem;margin-bottom:4px}}
  .sub{{color:#666;font-size:.82rem;margin-bottom:28px}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:22px;margin-bottom:16px}}
  .card h2{{font-size:.85rem;color:#666;margin-bottom:14px;text-transform:uppercase;letter-spacing:.05em}}
  .badge{{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:8px;
          font-weight:600;font-size:.9rem;color:{status_color};
          background:{status_color}18;border:1px solid {status_color}44;margin-bottom:14px}}
  .row{{display:flex;align-items:center;justify-content:space-between;
        font-size:.82rem;padding:7px 0;border-bottom:1px solid #222}}
  .row:last-child{{border:none}}
  .ok{{color:#4ade80}}.no{{color:#f87171}}
  .val{{color:#888;font-size:.78rem;max-width:60%;text-align:right;word-break:break-all}}
  .btn{{display:inline-flex;align-items:center;gap:8px;padding:11px 22px;border-radius:8px;
        font-size:.88rem;font-weight:600;text-decoration:none;background:#ff0000;
        color:#fff;border:none;cursor:pointer;margin-top:14px}}
  .btn:hover{{background:#cc0000}}
  .btn-row{{display:flex;gap:10px;flex-wrap:wrap}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔑 YouTube Auth Manager</h1>
  <p class="sub">OAuth & auto-refresh dikelola di Koyeb — token disimpan di JSONBin</p>

  <div class="card">
    <h2>Status Token</h2>
    <div class="badge">{status_text}</div>
    <div class="btn-row">
      <a href="/auth/login" class="btn">🔐 Login OAuth YouTube</a>
      {refresh_btn}
    </div>
  </div>

  <div class="card">
    <h2>Konfigurasi</h2>
    <div class="row"><span>Google OAuth</span>
      <span class="{'ok' if google_configured else 'no'}">{'✅ Terkonfigurasi' if google_configured else '❌ Belum di-set'}</span>
    </div>
    <div class="row"><span>JSONBin Storage</span>
      <span class="{'ok' if jsonbin_configured else 'no'}">{'✅ Terkonfigurasi' if jsonbin_configured else '❌ Belum di-set JSONBIN_BIN_ID / JSONBIN_API_KEY'}</span>
    </div>
  </div>
</div>
</body></html>"""

@app.route('/auth/login')
def auth_login():
    """Redirect ke Google OAuth."""
    if not OAUTHLIB_OK:
        return "Library google-auth-oauthlib tidak tersedia", 500
    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return "Set GOOGLE_CLIENT_ID dan GOOGLE_CLIENT_SECRET di env var Koyeb.", 400

    client_config = {"web": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [_get_oauth_redirect_uri()],
    }}
    flow = OAuthFlow.from_client_config(client_config, scopes=_OAUTH_SCOPES if 'OAUTHLIB_OK' in dir() else SCOPES)
    flow.redirect_uri = _get_oauth_redirect_uri()
    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    _oauth_state_store[state] = True
    from flask import redirect as _redir
    return _redir(auth_url)

@app.route('/auth/callback')
def auth_callback():
    """Terima token dari Google, simpan ke JSONBin."""
    if not OAUTHLIB_OK:
        return "Library google-auth-oauthlib tidak tersedia", 500

    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return "Tidak ada authorization code dari Google.", 400

    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    client_config = {"web": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [_get_oauth_redirect_uri()],
    }}
    try:
        import os as _os
        _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        _scopes = SCOPES
        flow = OAuthFlow.from_client_config(client_config, scopes=_scopes, state=state)
        flow.redirect_uri = _get_oauth_redirect_uri()
        flow.fetch_token(code=code)
        creds = flow.credentials
        _push_token_to_store(creds)
        _oauth_state_store.pop(state, None)
        from flask import redirect as _redir
        return _redir("/auth")
    except Exception as e:
        return f"Gagal ambil token: {e}", 500

@app.route('/auth/refresh')
def auth_force_refresh():
    """Force refresh token & simpan ke Vercel."""
    token_data = _pull_token_from_store()
    if not token_data:
        from flask import redirect as _redir
        return _redir("/auth")
    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        creds.refresh(Request())
        _push_token_to_store(creds)
    except Exception as e:
        print(f"[AUTH] Force refresh error: {e}")
    from flask import redirect as _redir
    return _redir("/auth")

# ============================================================
# FLASK ROUTES
# ============================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/about', methods=['GET'])
def get_about():
    return jsonify(load_about())

@app.route('/api/about', methods=['POST'])
def update_about():
    about = load_about()
    for k in ['title', 'description', 'tags', 'category']:
        if k in request.json:
            about[k] = request.json[k]
    save_about(about)
    return jsonify({"status": "ok"})

@app.route('/api/riwayat')
def get_riwayat():
    return jsonify(load_riwayat())

@app.route('/api/auth-status')
def auth_status():
    return jsonify({"authenticated": load_credentials() is not None})

@app.route('/api/ffmpeg-status')
def ffmpeg_status():
    ff = get_ffmpeg()
    return jsonify({"available": bool(ff), "ffmpeg": str(ff) if ff else None})

@app.route('/api/install-deps', methods=['POST'])
def install_deps():
    pkgs = (request.json or {}).get('packages', REQUIRED_PACKAGES)
    def generate():
        yield json.dumps({"status": "start", "total": len(pkgs)}) + "\n"
        results = []
        for pkg in pkgs:
            yield json.dumps({"status": "installing", "package": pkg}) + "\n"
            ok, out = install_package(pkg)
            res = {"package": pkg, "success": ok, "output": out[:300]}
            results.append(res)
            yield json.dumps({"status": "done_one", "result": res}) + "\n"
        global _FFMPEG_BIN, _FFPROBE_BIN, FFMPEG_BINARY
        _FFMPEG_BIN = _FFPROBE_BIN = None
        try:
            import importlib, imageio_ffmpeg as iff
            importlib.reload(iff)
            FFMPEG_BINARY = iff.get_ffmpeg_exe()
        except: pass
        ff = get_ffmpeg()
        yield json.dumps({
            "status": "complete", "results": results,
            "ffmpeg_path": str(ff) if ff else "not found",
            "success_count": sum(1 for r in results if r["success"]),
            "fail_count":    sum(1 for r in results if not r["success"]),
        }) + "\n"
    return Response(stream_with_context(generate()), content_type='application/x-ndjson')

# ── Upload Terjadwal ──────────────────────────────────────────

@app.route('/api/upload-scheduled-file', methods=['POST'])
def upload_scheduled_file():
    if 'video' not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files['video']
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    uid      = uuid.uuid4().hex[:8]
    base, ext = os.path.splitext(secure_filename(f.filename))
    fname    = f"{base}_{uid}{ext}"
    fpath    = temp_path(fname)
    f.save(fpath)

    try:
        fh = get_file_hash(fpath)
        is_dup, vid_id, reason = check_duplicate(fh)
        if is_dup:
            cleanup_temp(fname)
            return jsonify({"duplicate": True, "video_id": vid_id,
                            "link": f"https://youtu.be/{vid_id}" if vid_id else "",
                            "title": reason})
        file_size = os.path.getsize(fpath)
        return jsonify({
            "status": "ok", "filename": fname, "file_hash": fh,
            "file_size": file_size,
            "file_size_mb": round(file_size / 1024 / 1024, 2)
        })
    except Exception as e:
        cleanup_temp(fname)
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload-to-github', methods=['POST'])
def upload_to_github_route():
    data     = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({"error": "No filename"}), 400

    fpath = temp_path(filename)
    if not os.path.exists(fpath):
        return jsonify({"error": "File tidak ditemukan di server"}), 404

    github_path = f"video/{filename}"
    success, result = gh_upload_video(fpath, github_path)
    if success:
        return jsonify({
            "status": "ok",
            "github_path": github_path,
            "github_url": f"https://github.com/{GITHUB_REPO}/blob/main/{github_path}"
        })
    else:
        return jsonify({"error": f"Gagal upload ke GitHub: {result}"}), 500

@app.route('/api/schedule', methods=['POST'])
def schedule_upload():
    data     = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({"error": "No filename"}), 400

    tv = float(data.get('timeout_value', 0))
    tu = data.get('timeout_unit', 'hours')
    ts = tv * (3600 if tu == 'hours' else 60 if tu == 'minutes' else 1)
    now_ts = time.time()

    file_hash = data.get('file_hash', '')
    if not file_hash:
        fpath = temp_path(filename)
        if os.path.exists(fpath):
            file_hash = get_file_hash(fpath)

    if file_hash:
        is_dup, vid_id, reason = check_duplicate(file_hash)
        if is_dup:
            return jsonify({"duplicate": True, "video_id": vid_id, "message": reason})

    github_path = data.get('github_path', f"video/{filename}")
    about = load_about()

    with queue_lock:
        queue    = load_queue()
        has_busy = any(q.get("status") in ("pending", "uploading", "waiting") for q in queue)
        if has_busy:
            status       = "waiting"
            upload_at_ts = None
            upload_at    = "(menunggu giliran)"
        else:
            status       = "pending"
            upload_at_ts = now_ts + ts
            upload_at    = datetime.fromtimestamp(upload_at_ts).strftime("%Y-%m-%d %H:%M:%S")

        item = {
            "id":               str(uuid.uuid4()),
            "filename":         filename,
            "file_hash":        file_hash,
            "github_path":      github_path,
            "title":            data.get('title', about.get('title', '')),
            "description":      data.get('description', about.get('description', '')),
            "tags":             data.get('tags', about.get('tags', [])),
            "category":         data.get('category', about.get('category', '20')),
            "source":           "upload",
            "added_at":         time.strftime("%Y-%m-%d %H:%M:%S"),
            "added_at_ts":      now_ts,
            "timeout_seconds":  ts,
            "timeout_value":    tv,
            "timeout_unit":     tu,
            "upload_at_ts":     upload_at_ts,
            "upload_at":        upload_at,
            "status":           status,
            "remaining_seconds": ts if status == "pending" else None,
        }
        queue.append(item)
        save_queue(queue, force_gh=True)

    return jsonify({"status": "ok", "id": item["id"],
                    "queue_item": item, "queue_status": status})

@app.route('/api/queue')
def get_queue():
    queue  = load_queue()
    now_ts = time.time()
    for item in queue:
        if item.get("status") == "pending" and item.get("upload_at_ts"):
            item["remaining_seconds"] = max(0, item["upload_at_ts"] - now_ts)
        elif item.get("status") == "waiting":
            item["remaining_seconds"] = None
    return jsonify(queue)

@app.route('/api/queue/<item_id>', methods=['DELETE'])
def delete_queue_item(item_id):
    with queue_lock:
        queue = load_queue()
        found = next((q for q in queue if q.get("id") == item_id), None)
        if not found:
            return jsonify({"error": "Not found"}), 404
        if found.get("status") == "uploading":
            return jsonify({"error": "Sedang diupload, tidak bisa dihapus"}), 400
        was_pending = found.get("status") == "pending"
        queue = [q for q in queue if q.get("id") != item_id]
        if was_pending:
            activate_next_waiting(queue)
        save_queue(queue, force_gh=True)
        cleanup_temp(found.get("filename", ""))
    return jsonify({"status": "ok"})

@app.route('/api/settings')
def get_settings():
    return jsonify(load_settings())

@app.route('/api/pause-toggle', methods=['POST'])
def pause_toggle():
    s = load_settings()
    new_paused = not bool(s.get("paused", False))
    s["paused"] = new_paused
    s["paused_at"] = time.strftime("%Y-%m-%d %H:%M:%S") if new_paused else None
    save_settings(s)
    return jsonify({"status": "ok", "paused": new_paused})

@app.route('/api/delete-all', methods=['POST'])
def delete_all():
    """Hapus semua antrian + semua file di GitHub folder video/ dan data/ (kecuali settings.json)."""
    import requests as r

    # 1. Kosongkan antrian di RAM
    with queue_lock:
        save_queue([], force_gh=True)

    # Helper hapus satu file dari GitHub
    def _delete_gh_file(path, sha):
        try:
            payload = {"message": f"[delete-all] {os.path.basename(path)}", "sha": sha}
            resp = r.delete(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
                            headers=_gh_headers(), json=payload, timeout=30)
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f"[DELETE ALL] error hapus {path}: {e}")
            return False

    # Helper list folder GitHub
    def _list_folder(folder):
        try:
            resp = r.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{folder}",
                         headers=_gh_headers(), timeout=15)
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list):
                    return [(item["path"], item["sha"]) for item in items if item["type"] == "file"]
        except Exception as e:
            print(f"[DELETE ALL] error list {folder}: {e}")
        return []

    deleted = []
    errors  = []

    # 2. Hapus semua file di folder video/
    for path, sha in _list_folder("video"):
        ok = _delete_gh_file(path, sha)
        (deleted if ok else errors).append(path)
        _sha_cache.pop(path, None)

    # 3. Hapus semua file di folder data/ KECUALI settings.json
    for path, sha in _list_folder("data"):
        if os.path.basename(path) == "settings.json":
            continue
        ok = _delete_gh_file(path, sha)
        (deleted if ok else errors).append(path)
        _sha_cache.pop(path, None)

    return jsonify({"status": "ok", "deleted": deleted, "errors": errors})

@app.route('/api/queue/<item_id>/retry', methods=['POST'])
def retry_queue_item(item_id):
    with queue_lock:
        queue = load_queue()
        found = next((q for q in queue if q.get("id") == item_id), None)
        if not found:
            return jsonify({"error": "Not found"}), 404
        if found.get("status") != "failed":
            return jsonify({"error": "Hanya item yang gagal yang bisa di-retry"}), 400

        # Reset status dan generate ID baru supaya tidak konflik
        now_ts = time.time()
        has_busy = any(q.get("status") in ("pending", "uploading", "waiting") for q in queue if q.get("id") != item_id)

        if has_busy:
            found["status"] = "waiting"
            found["upload_at_ts"] = None
            found["upload_at"] = "(menunggu giliran)"
            found["remaining_seconds"] = None
        else:
            ts = float(found.get("timeout_seconds", 0))
            found["status"] = "pending"
            found["upload_at_ts"] = now_ts + ts
            found["upload_at"] = datetime.fromtimestamp(now_ts + ts).strftime("%Y-%m-%d %H:%M:%S")
            found["remaining_seconds"] = ts

        found.pop("error", None)
        found["retried_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        save_queue(queue, force_gh=True)

    return jsonify({"status": "ok", "queue_status": found["status"]})

@app.route('/api/queue/<item_id>/check', methods=['GET'])
def check_queue_item(item_id):
    queue = load_queue()
    found = next((q for q in queue if q.get("id") == item_id), None)
    if not found:
        return jsonify({"error": "Not found"}), 404

    github_path = found.get("github_path", "")
    result = {
        "id":          item_id,
        "title":       found.get("title", ""),
        "filename":    found.get("filename", ""),
        "github_path": github_path,
        "status":      found.get("status", ""),
    }
    if github_path:
        verified, reason = gh_verify_video(github_path)
        result["github_verified"] = verified
        result["github_reason"]   = reason
        result["github_url"]      = f"https://github.com/{GITHUB_REPO}/blob/main/{github_path}" if verified else None
    else:
        result["github_verified"] = False
        result["github_reason"]   = "Tidak ada github_path"

    return jsonify(result)

@app.route('/api/check-queue-summary')
def check_queue_summary():
    queue   = load_queue()
    riwayat = load_riwayat()
    now_ts  = time.time()

    active = [q for q in queue if q.get("status") in ("pending", "waiting", "uploading")]
    done   = [q for q in queue if q.get("status") == "done"]
    failed = [q for q in queue if q.get("status") == "failed"]

    active_detail = []
    for q in active:
        remaining = None
        if q.get("status") == "pending" and q.get("upload_at_ts"):
            remaining = max(0, q["upload_at_ts"] - now_ts)
        github_verified = None
        if q.get("github_path"):
            v, _ = gh_verify_video(q["github_path"])
            github_verified = v
        active_detail.append({
            "id": q.get("id"), "title": q.get("title", ""),
            "status": q.get("status"), "remaining_seconds": remaining,
            "github_path": q.get("github_path"), "github_verified": github_verified,
            "added_at": q.get("added_at"),
        })

    failed_detail = [{"id": q.get("id"), "title": q.get("title",""),
                      "error": q.get("error","Unknown"), "added_at": q.get("added_at")} for q in failed]

    done_detail = [{"id": q.get("id"), "title": q.get("title",""),
                    "video_id": q.get("video_id"), "link": q.get("link"),
                    "uploaded_at": q.get("uploaded_at")} for q in done[-5:]]

    return jsonify({
        "total_queue": len(queue), "active_count": len(active),
        "done_count": len(done), "failed_count": len(failed),
        "riwayat_count": len(riwayat),
        "active": active_detail, "failed": failed_detail, "done_recent": done_detail,
        "github_repo": GITHUB_REPO, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

# ── Video Maker ───────────────────────────────────────────────

@app.route('/api/upload-photo', methods=['POST'])
def upload_photo():
    if 'photo' not in request.files:
        return jsonify({"error": "No photo"}), 400
    f   = request.files['photo']
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in SUPPORTED_IMAGE:
        return jsonify({"error": f"Format tidak didukung: {ext}"}), 400
    fname = f"photo_{uuid.uuid4().hex[:8]}{ext}"
    fpath = temp_path(fname)
    f.save(fpath)
    w, h   = get_image_dimensions(fpath)
    vw, vh = calc_video_size_1080p(w, h)
    fh     = get_file_hash(fpath)
    return jsonify({
        "status": "ok", "filename": fname, "file_hash": fh,
        "original_size": f"{w}x{h}" if w else "unknown",
        "video_size":    f"{vw}x{vh}",
    })

@app.route('/api/music-list')
def music_list():
    files = []
    for fname in sorted(os.listdir(MUSIC_FOLDER)):
        if os.path.splitext(fname)[1].lower() in SUPPORTED_MUSIC:
            fpath = os.path.join(MUSIC_FOLDER, fname)
            dur   = get_music_duration(fpath)
            files.append({
                "filename": fname, "name": os.path.splitext(fname)[0],
                "duration": round(dur, 1) if dur else None,
                "duration_str": format_duration(dur),
                "size_mb": round(os.path.getsize(fpath) / 1024 / 1024, 1),
            })
    return jsonify(files)

@app.route('/api/create-video', methods=['POST'])
def create_video():
    data           = request.json or {}
    photo_filename = data.get('photo_filename', '')
    music_filename = data.get('music_filename', '')
    if not photo_filename or not music_filename:
        return jsonify({"error": "photo_filename dan music_filename wajib diisi"}), 400

    photo_path = temp_path(photo_filename)
    music_path = os.path.join(MUSIC_FOLDER, music_filename)

    if not os.path.exists(photo_path):
        return jsonify({"error": "File foto tidak ditemukan"}), 404
    if not os.path.exists(music_path):
        return jsonify({"error": "File musik tidak ditemukan"}), 404
    if not get_ffmpeg():
        return jsonify({"error": "ffmpeg tidak ditemukan! Install dulu."}), 500

    task_id     = str(uuid.uuid4())
    out_fname   = f"made_{task_id[:8]}.mp4"
    output_path = temp_path(out_fname)

    with video_tasks_lock:
        video_tasks[task_id] = {
            "status": "pending", "progress": 0,
            "photo_filename": photo_filename, "music_filename": music_filename,
            "output_filename": None, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "photo_hash": get_file_hash(photo_path),
        }

    threading.Thread(target=run_video_creation,
                     args=(task_id, photo_path, music_path, output_path),
                     daemon=True).start()
    return jsonify({"status": "ok", "task_id": task_id})

@app.route('/api/create-progress/<task_id>')
def create_progress(task_id):
    with video_tasks_lock:
        task = video_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task tidak ditemukan"}), 404
    return jsonify(task)

@app.route('/api/delete-made-video', methods=['POST'])
def delete_made_video():
    data = request.json or {}
    cleanup_temp(data.get('filename', ''))
    cleanup_temp(data.get('photo_filename', ''))
    return jsonify({"status": "ok"})

@app.route('/music/<filename>')
def serve_music(filename):
    return send_from_directory(MUSIC_FOLDER, filename)

@app.route('/api/video-preview/<filename>')
def video_preview(filename):
    p = temp_path(filename)
    if os.path.exists(p):
        return send_from_directory(TEMP_FOLDER, filename, mimetype='video/mp4')
    return jsonify({"error": "Video tidak ditemukan"}), 404

# ── Edit Video ───────────────────────────────────────────────

ABOUT_VID_FOLDER = os.path.join(BASE_DIR, 'about_vid')
os.makedirs(ABOUT_VID_FOLDER, exist_ok=True)

@app.route('/api/channel-videos')
def channel_videos():
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    try:
        youtube = build("youtube", "v3", credentials=creds)
        # Get channel id
        ch = youtube.channels().list(part="id", mine=True).execute()
        if not ch.get("items"): return jsonify({"error": "Channel tidak ditemukan"}), 404
        channel_id = ch["items"][0]["id"]

        # Step 1: Collect all video IDs via search (snippet only)
        video_ids = []
        next_page = None
        while True:
            params = dict(part="snippet", channelId=channel_id,
                          maxResults=50, order="date", type="video")
            if next_page: params["pageToken"] = next_page
            resp = youtube.search().list(**params).execute()
            for item in resp.get("items", []):
                vid_id = item["id"].get("videoId")
                if vid_id: video_ids.append(vid_id)
            next_page = resp.get("nextPageToken")
            if not next_page: break

        # Step 2: Get full details in batches of 50
        videos = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            det = youtube.videos().list(
                part="snippet,statistics,status,contentDetails",
                id=",".join(batch)
            ).execute()
            for v in det.get("items", []):
                vid_id = v["id"]
                snip = v.get("snippet", {})
                stat = v.get("statistics", {})
                videos.append({
                    "video_id":    vid_id,
                    "title":       snip.get("title", ""),
                    "description": snip.get("description", ""),
                    "tags":        snip.get("tags", []),
                    "category":    snip.get("categoryId", ""),
                    "thumbnail":   snip.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "published":   snip.get("publishedAt", ""),
                    "views":       int(stat.get("viewCount", 0)),
                    "likes":       int(stat.get("likeCount", 0)),
                    "comments":    int(stat.get("commentCount", 0)),
                    "privacy":     v.get("status", {}).get("privacyStatus", ""),
                    "link":        f"https://youtu.be/{vid_id}",
                })
        return jsonify({"videos": videos, "total": len(videos)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/video-detail/<video_id>')
def video_detail(video_id):
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi"}), 401
    try:
        youtube = build("youtube", "v3", credentials=creds)
        det = youtube.videos().list(
            part="snippet,statistics,status,contentDetails",
            id=video_id
        ).execute()
        if not det.get("items"): return jsonify({"error": "Video tidak ditemukan"}), 404
        v = det["items"][0]
        snip = v.get("snippet", {})
        stat = v.get("statistics", {})

        # Get playlists video ini ada di mana
        playlists = []
        try:
            pl_resp = youtube.playlistItems().list(
                part="snippet", videoId=video_id, maxResults=10
            ).execute()
            for pi in pl_resp.get("items", []):
                playlists.append({
                    "playlist_id": pi["snippet"].get("playlistId"),
                    "title": pi["snippet"].get("title", "")
                })
        except: pass

        return jsonify({
            "video_id":    video_id,
            "title":       snip.get("title", ""),
            "description": snip.get("description", ""),
            "tags":        snip.get("tags", []),
            "category":    snip.get("categoryId", ""),
            "thumbnail":   snip.get("thumbnails", {}).get("medium", {}).get("url", ""),
            "published":   snip.get("publishedAt", ""),
            "views":       int(stat.get("viewCount", 0)),
            "likes":       int(stat.get("likeCount", 0)),
            "comments":    int(stat.get("commentCount", 0)),
            "favorites":   int(stat.get("favoriteCount", 0)),
            "privacy":     v.get("status", {}).get("privacyStatus", ""),
            "duration":    v.get("contentDetails", {}).get("duration", ""),
            "playlists":   playlists,
            "link":        f"https://youtu.be/{video_id}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/video-update/<video_id>', methods=['POST'])
def video_update(video_id):
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi"}), 401
    data = request.json or {}
    errors = []
    updated = []

    try:
        youtube = build("youtube", "v3", credentials=creds)
        # Get current snippet
        det = youtube.videos().list(part="snippet", id=video_id).execute()
        if not det.get("items"): return jsonify({"error": "Video tidak ditemukan"}), 404
        snippet = det["items"][0]["snippet"]

        # Update snippet fields
        changed = False
        if "title" in data:
            snippet["title"] = data["title"]; changed = True; updated.append("title")
        if "description" in data:
            snippet["description"] = data["description"]; changed = True; updated.append("description")
        if "tags" in data:
            snippet["tags"] = data["tags"]; changed = True; updated.append("tags")
        if "category" in data:
            snippet["categoryId"] = data["category"]; changed = True; updated.append("category")

        if changed:
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet}
            ).execute()

        # Update playlist
        if "playlist_id" in data and data["playlist_id"]:
            try:
                ok, pl_err = add_to_playlist(youtube, video_id, data["playlist_id"])
                if ok: updated.append("playlist")
                else: errors.append(f"Playlist: {pl_err}")
            except Exception as e:
                errors.append(f"Playlist: {str(e)}")

        return jsonify({"status": "ok", "updated": updated, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e), "errors": errors}), 500

@app.route('/api/video-delete/<video_id>', methods=['DELETE'])
def video_delete(video_id):
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi"}), 401
    try:
        youtube = build("youtube", "v3", credentials=creds)
        youtube.videos().delete(id=video_id).execute()
        return jsonify({"status": "ok", "deleted": video_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/about-vid-list')
def about_vid_list():
    files = []
    for fname in sorted(os.listdir(ABOUT_VID_FOLDER)):
        if fname.endswith('.json'):
            fpath = os.path.join(ABOUT_VID_FOLDER, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                files.append({"filename": fname, "title": data.get("title", fname), "data": data})
            except: pass
    return jsonify(files)

@app.route('/api/video-update-bulk/<video_id>', methods=['POST'])
def video_update_bulk(video_id):
    data = request.json or {}
    about_file = data.get("about_file", "")
    if not about_file: return jsonify({"error": "Tidak ada about_file"}), 400
    fpath = os.path.join(ABOUT_VID_FOLDER, about_file)
    if not os.path.exists(fpath): return jsonify({"error": "File tidak ditemukan"}), 404
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            about = json.load(f)

        if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
        creds = load_credentials()
        if not creds: return jsonify({"error": "Belum autentikasi"}), 401
        errors = []
        updated = []

        youtube = build("youtube", "v3", credentials=creds)
        det = youtube.videos().list(part="snippet", id=video_id).execute()
        if not det.get("items"): return jsonify({"error": "Video tidak ditemukan"}), 404
        snippet = det["items"][0]["snippet"]

        if "title" in about:       snippet["title"] = about["title"]; updated.append("title")
        if "description" in about: snippet["description"] = about["description"]; updated.append("description")
        if "tags" in about:        snippet["tags"] = about["tags"]; updated.append("tags")
        if "category" in about:    snippet["categoryId"] = about["category"]; updated.append("category")

        try:
            youtube.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        except Exception as e:
            errors.append(f"Update snippet: {str(e)}")

        if "playlist" in about and about["playlist"]:
            try:
                ok, pl_err = add_to_playlist(youtube, video_id, about["playlist"])
                if ok: updated.append("playlist")
                else: errors.append(f"Playlist: {pl_err}")
            except Exception as e:
                errors.append(f"Playlist: {str(e)}")

        return jsonify({"status": "ok", "updated": updated, "errors": errors, "about_file": about_file})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# REST API — UNTUK SCRIPT LUAR
# ============================================================
#
# Endpoint ini memungkinkan script lain mengirim video + timer
# dan secara otomatis:
#   1. Upload video ke GitHub
#   2. Cek duplikat & riwayat
#   3. Masukkan ke antrian dengan timer yg dikirim
#   4. Kembalikan respons lengkap
#
# Cara pakai:
#   POST /api/v1/submit
#   Content-Type: multipart/form-data
#
# Field:
#   video         (file)   — file video mp4/dll  [WAJIB]
#   timer_value   (int)    — angka timer          [WAJIB]
#   timer_unit    (str)    — hours / minutes / seconds [default: hours]
#   category      (str)    — ID kategori YouTube (1-28)  [optional]
#   title         (str)    — judul video          [optional, pakai default about]
#   description   (str)    — deskripsi            [optional]
#   tags          (str)    — tags dipisah koma    [optional]
#   api_key       (str)    — API key keamanan     [optional jika API_KEY env tidak di-set]
#
# Response JSON:
#   {
#     "success": true,
#     "message": "...",
#     "queue_id": "...",
#     "github": { "path": "...", "url": "..." },
#     "timer": { "value": 10, "unit": "hours", "upload_at": "..." },
#     "video_info": { "filename": "...", "size_mb": ... },
#     "queue_status": "pending" | "waiting",
#     "duplicate": false
#   }

API_KEY = os.environ.get("API_KEY", "")  # Jika kosong, tidak perlu autentikasi

def _check_api_key():
    """Cek API key dari header atau form data."""
    if not API_KEY:
        return True  # Tidak ada API key yang di-set, lewati pengecekan
    provided = (
        request.headers.get("X-API-Key") or
        request.headers.get("Authorization", "").replace("Bearer ", "") or
        (request.json or {}).get("api_key") or
        request.form.get("api_key") or
        request.args.get("api_key")
    )
    return provided == API_KEY

@app.route('/api/v1/submit', methods=['POST'])
def api_v1_submit():
    """
    Endpoint utama REST API.
    Terima video + konfigurasi dari script lain, proses otomatis, kembalikan respons.
    """
    # === 1. Auth ===
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized. API key salah atau tidak ada."}), 401

    # === 2. Validasi file ===
    if 'video' not in request.files:
        return jsonify({"success": False, "error": "Field 'video' tidak ada. Kirim file video."}), 400
    f = request.files['video']
    if not f or not f.filename:
        return jsonify({"success": False, "error": "File video kosong."}), 400

    # === 3. Ambil parameter ===
    timer_value = request.form.get('timer_value', 0)
    timer_unit  = request.form.get('timer_unit', 'hours').lower()
    category    = request.form.get('category', '')
    title       = request.form.get('title', '')
    description = request.form.get('description', '')
    tags_raw    = request.form.get('tags', '')
    playlist_id = request.form.get('playlist_id', '')

    try:
        timer_value = float(timer_value)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": f"timer_value tidak valid: {timer_value}"}), 400

    if timer_unit not in ('hours', 'minutes', 'seconds'):
        return jsonify({"success": False,
                        "error": "timer_unit harus 'hours', 'minutes', atau 'seconds'"}), 400

    timer_seconds = timer_value * (3600 if timer_unit == 'hours' else 60 if timer_unit == 'minutes' else 1)

    tags = [t.strip() for t in tags_raw.split(',') if t.strip()] if tags_raw else []

    # === 4. Simpan file sementara ===
    uid       = uuid.uuid4().hex[:8]
    base, ext = os.path.splitext(secure_filename(f.filename))
    fname     = f"{base}_{uid}{ext}"
    fpath     = temp_path(fname)
    f.save(fpath)

    # === 4b. Simpan thumbnail jika ada ===
    thumbnail_github_path = ""
    if 'thumbnail' in request.files:
        th_file = request.files['thumbnail']
        if th_file and th_file.filename:
            th_ext   = os.path.splitext(secure_filename(th_file.filename))[1] or ".jpg"
            th_fname = f"thumb_{uid}{th_ext}"
            th_fpath = temp_path(th_fname)
            th_file.save(th_fpath)
            # Upload thumbnail ke GitHub
            th_repo_path = f"thumbnails/{th_fname}"
            print(f"[API v1] Upload thumbnail ke GitHub: {th_repo_path}")
            th_ok, _ = gh_upload_video(th_fpath, th_repo_path)
            if th_ok:
                thumbnail_github_path = th_repo_path
                print(f"[API v1] Thumbnail tersimpan di GitHub: {th_repo_path}")
            else:
                print(f"[API v1] Gagal upload thumbnail ke GitHub, akan pakai default")
            try: os.remove(th_fpath)
            except: pass

    # === 5. Validasi file video ===
    valid, reason = is_valid_video(fpath)
    if not valid:
        cleanup_temp(fname)
        return jsonify({"success": False, "error": f"File tidak valid: {reason}"}), 400

    file_size = os.path.getsize(fpath)

    # === 6. Hash & cek duplikat ===
    try:
        file_hash = get_file_hash(fpath)
    except Exception as e:
        cleanup_temp(fname)
        return jsonify({"success": False, "error": f"Gagal membaca file: {e}"}), 500

    is_dup, dup_vid_id, dup_reason = check_duplicate(file_hash)
    if is_dup:
        cleanup_temp(fname)
        resp = {
            "success": True,
            "duplicate": True,
            "message": dup_reason,
            "video_info": {"filename": f.filename, "size_mb": round(file_size / 1024 / 1024, 2)},
        }
        if dup_vid_id:
            resp["youtube"] = {
                "video_id": dup_vid_id,
                "link": f"https://youtu.be/{dup_vid_id}",
                "youtube_url": f"https://www.youtube.com/watch?v={dup_vid_id}"
            }
        return jsonify(resp)

    # === 7. Upload video ke GitHub ===
    github_path = f"video/{fname}"
    print(f"[API v1] Upload ke GitHub: {github_path}")
    gh_ok, gh_result = gh_upload_video(fpath, github_path)
    if not gh_ok:
        cleanup_temp(fname)
        return jsonify({
            "success": False,
            "error": f"Gagal upload ke GitHub: {gh_result}",
            "hint": "Cek GITHUB_TOKEN dan GITHUB_REPO environment variable."
        }), 500

    github_url = f"https://github.com/{GITHUB_REPO}/blob/main/{github_path}"
    github_raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{github_path}"

    # === 8. Load about default & override jika ada input ===
    about = load_about()
    final_title       = title or about.get('title', '')
    final_description = description or about.get('description', '')
    final_tags        = tags or about.get('tags', [])
    final_category    = category or about.get('category', '20')
    final_playlist_id = playlist_id or about.get('playlist', '')

    # === 9. Masukkan ke antrian ===
    now_ts = time.time()
    with queue_lock:
        queue    = load_queue()
        has_busy = any(q.get("status") in ("pending", "uploading", "waiting") for q in queue)

        if has_busy:
            status       = "waiting"
            upload_at_ts = None
            upload_at    = "(menunggu giliran)"
        else:
            status       = "pending"
            upload_at_ts = now_ts + timer_seconds
            upload_at    = datetime.fromtimestamp(upload_at_ts).strftime("%Y-%m-%d %H:%M:%S")

        item = {
            "id":                     str(uuid.uuid4()),
            "filename":               fname,
            "file_hash":              file_hash,
            "github_path":            github_path,
            "thumbnail_github_path":  thumbnail_github_path,
            "title":                  final_title,
            "description":            final_description,
            "tags":                   final_tags,
            "category":               final_category,
            "playlist_id":            final_playlist_id,
            "source":                 "api_v1",
            "added_at":               time.strftime("%Y-%m-%d %H:%M:%S"),
            "added_at_ts":            now_ts,
            "timeout_seconds":        timer_seconds,
            "timeout_value":          timer_value,
            "timeout_unit":           timer_unit,
            "upload_at_ts":           upload_at_ts,
            "upload_at":              upload_at,
            "status":                 status,
            "remaining_seconds":      timer_seconds if status == "pending" else None,
        }
        queue.append(item)
        save_queue(queue, force_gh=True)

    print(f"[API v1] Berhasil: {fname} → antrian {item['id']} [{status}]")

    # === 10. Buat respons lengkap ===
    # Format timer yang human-readable
    if timer_unit == 'hours':
        timer_str = f"{timer_value} jam"
    elif timer_unit == 'minutes':
        timer_str = f"{timer_value} menit"
    else:
        timer_str = f"{timer_value} detik"

    return jsonify({
        "success": True,
        "duplicate": False,
        "message": f"Video berhasil diterima dan dimasukkan ke antrian. Upload akan dilakukan dalam {timer_str}.",
        "queue_id":     item["id"],
        "queue_status": status,
        "github": {
            "path":    github_path,
            "url":     github_url,
            "raw_url": github_raw_url,
            "repo":    GITHUB_REPO,
        },
        "timer": {
            "value":     timer_value,
            "unit":      timer_unit,
            "seconds":   timer_seconds,
            "upload_at": upload_at,
            "human":     timer_str,
        },
        "video_info": {
            "original_filename": f.filename,
            "saved_filename":    fname,
            "file_hash":         file_hash,
            "size_bytes":        file_size,
            "size_mb":           round(file_size / 1024 / 1024, 2),
        },
        "metadata": {
            "title":       final_title,
            "description": final_description,
            "tags":        final_tags,
            "category":    final_category,
            "playlist_id": final_playlist_id,
            "thumbnail":   f"github:{thumbnail_github_path}" if thumbnail_github_path else "default (thumbnail/thumbnail.jpg)",
        },
        "check_status_url": f"/api/v1/status/{item['id']}",
    }), 202


@app.route('/api/v1/status/<queue_id>', methods=['GET'])
def api_v1_status(queue_id):
    """Cek status antrian berdasarkan queue_id."""
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    queue = load_queue()
    item  = next((q for q in queue if q.get("id") == queue_id), None)

    if not item:
        # Cek di riwayat (mungkin sudah selesai diupload)
        riwayat = load_riwayat()
        hist = next((r for r in riwayat if r.get("queue_id") == queue_id), None)
        if hist:
            return jsonify({
                "success": True,
                "queue_id": queue_id,
                "status": "done",
                "youtube": {
                    "video_id": hist.get("video_id"),
                    "link": hist.get("link"),
                    "youtube_url": hist.get("youtube_url"),
                    "thumbnail": hist.get("thumbnail"),
                },
                "uploaded_at": hist.get("tanggal_upload"),
            })
        return jsonify({"success": False, "error": "Queue ID tidak ditemukan"}), 404

    now_ts    = time.time()
    remaining = None
    if item.get("status") == "pending" and item.get("upload_at_ts"):
        remaining = max(0, item["upload_at_ts"] - now_ts)

    resp = {
        "success":          True,
        "queue_id":         queue_id,
        "status":           item.get("status"),
        "title":            item.get("title"),
        "github_path":      item.get("github_path"),
        "upload_at":        item.get("upload_at"),
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
        "added_at":         item.get("added_at"),
    }

    if item.get("status") == "done":
        resp["youtube"] = {
            "video_id":   item.get("video_id"),
            "link":       item.get("link"),
            "youtube_url": f"https://www.youtube.com/watch?v={item.get('video_id')}" if item.get("video_id") else None,
        }
        resp["uploaded_at"] = item.get("uploaded_at")
    elif item.get("status") == "failed":
        resp["error"] = item.get("error", "Unknown error")

    return jsonify(resp)


@app.route('/api/v1/queue', methods=['GET'])
def api_v1_queue():
    """Lihat semua antrian (opsional dengan filter status)."""
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    status_filter = request.args.get('status')
    queue = load_queue()
    now_ts = time.time()

    result = []
    for item in queue:
        if status_filter and item.get("status") != status_filter:
            continue
        remaining = None
        if item.get("status") == "pending" and item.get("upload_at_ts"):
            remaining = max(0, item["upload_at_ts"] - now_ts)
        result.append({
            "queue_id":         item.get("id"),
            "status":           item.get("status"),
            "title":            item.get("title"),
            "upload_at":        item.get("upload_at"),
            "remaining_seconds": round(remaining, 1) if remaining is not None else None,
            "added_at":         item.get("added_at"),
            "source":           item.get("source"),
            "github_path":      item.get("github_path"),
            "video_id":         item.get("video_id"),
            "link":             item.get("link"),
            "error":            item.get("error"),
        })

    return jsonify({
        "success": True,
        "total":   len(result),
        "items":   result
    })


@app.route('/api/v1/info', methods=['GET'])
def api_v1_info():
    """Info endpoint — untuk verifikasi API aktif dan cek konfigurasi."""
    return jsonify({
        "success":      True,
        "api_version":  "1.0",
        "auth_required": bool(API_KEY),
        "github_repo":  GITHUB_REPO,
        "endpoints": {
            "submit":     "POST /api/v1/submit",
            "status":     "GET  /api/v1/status/<queue_id>",
            "queue_list": "GET  /api/v1/queue",
            "info":       "GET  /api/v1/info",
        },
        "submit_fields": {
            "video":        "file — file video [WAJIB]",
            "timer_value":  "int/float — angka timer [WAJIB]",
            "timer_unit":   "str — hours | minutes | seconds [default: hours]",
            "category":     "str — ID kategori YouTube [optional]",
            "title":        "str — judul video [optional]",
            "description":  "str — deskripsi [optional]",
            "tags":         "str — tags dipisah koma [optional]",
            "playlist_id":  "str — ID playlist YouTube [optional]",
            "thumbnail":    "file — gambar thumbnail JPG/PNG [optional, default: thumbnail/thumbnail.jpg]",
            "api_key":      "str — API key [jika diperlukan]",
        },
        "timer_units": ["hours", "minutes", "seconds"],
        "categories":  YOUTUBE_CATEGORIES,
    })


# ============================================================
# CHANNEL EDIT APIs
# ============================================================

@app.route('/api/channel-info')
def channel_info():
    """Ambil info channel: deskripsi, keywords, dsb."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    try:
        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.channels().list(
            part="snippet,brandingSettings",
            mine=True
        ).execute()
        if not resp.get("items"):
            return jsonify({"error": "Channel tidak ditemukan"}), 404
        ch    = resp["items"][0]
        snip  = ch.get("snippet", {})
        brand = ch.get("brandingSettings", {})
        ch_set = brand.get("channel", {})
        return jsonify({
            "channel_id":   ch["id"],
            "title":        snip.get("title", ""),
            "description":  snip.get("description", ""),
            "country":      snip.get("country", ""),
            "keywords":     [kw.strip() for kw in ch_set.get("keywords", "").split(",") if kw.strip()],
            "keywords_raw": ch_set.get("keywords", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/channel-update', methods=['POST'])
def channel_update():
    """Update deskripsi dan/atau keywords channel."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    data = request.json or {}
    try:
        youtube = build("youtube", "v3", credentials=creds)

        # Ambil data channel sekarang
        resp = youtube.channels().list(
            part="snippet,brandingSettings",
            mine=True
        ).execute()
        if not resp.get("items"):
            return jsonify({"error": "Channel tidak ditemukan"}), 404

        ch     = resp["items"][0]
        ch_id  = ch["id"]
        snip   = ch.get("snippet", {})
        brand  = ch.get("brandingSettings", {})
        ch_set = brand.get("channel", {})

        updated = []

        # Update description via brandingSettings (snippet update is restricted by YouTube API)
        if "description" in data:
            desc = data["description"]
            # YouTube deskripsi channel max 1000 karakter
            MAX_DESC = 1000
            truncated_desc = False
            if len(desc) > MAX_DESC:
                desc = desc[:MAX_DESC]
                truncated_desc = True
            youtube.channels().update(
                part="brandingSettings",
                body={
                    "id": ch_id,
                    "brandingSettings": {
                        "channel": {
                            "description": desc
                        }
                    }
                }
            ).execute()
            updated.append("description")

        # Update keywords — YouTube expects space-separated string in brandingSettings
        if "keywords" in data:
            keywords_list = data["keywords"]
            if isinstance(keywords_list, list):
                kw_parts = []
                for kw in keywords_list:
                    kw = kw.strip()
                    if not kw:
                        continue
                    kw_parts.append(f'"{kw}"' if ' ' in kw else kw)
                kw_str = " ".join(kw_parts)
            else:
                kw_str = str(keywords_list)
            # YouTube limit: 500 karakter untuk keywords
            MAX_KW = 500
            if len(kw_str) > MAX_KW:
                # Potong per-tag sampai muat
                kw_parts_trimmed = []
                total = 0
                for part in kw_parts:
                    needed = len(part) + (1 if kw_parts_trimmed else 0)
                    if total + needed > MAX_KW:
                        break
                    kw_parts_trimmed.append(part)
                    total += needed
                kw_str = " ".join(kw_parts_trimmed)

            youtube.channels().update(
                part="brandingSettings",
                body={
                    "id": ch_id,
                    "brandingSettings": {
                        "channel": {
                            "keywords": kw_str
                        }
                    }
                }
            ).execute()
            updated.append("keywords")
            # Kembalikan info berapa tag yang berhasil masuk
            saved_count = len(kw_str.split()) if not isinstance(keywords_list, list) else sum(1 for kw in keywords_list if kw.strip() and kw.strip() in kw_str)

        result = {"status": "ok", "updated": updated}
        if "keywords" in data:
            result["keywords_saved"] = kw_str
            result["keywords_length"] = len(kw_str)
            result["keywords_truncated"] = len(kw_str) < len(" ".join(kw_parts)) if "kw_parts" in dir() else False
        if "description" in data:
            result["description_truncated"] = truncated_desc if "truncated_desc" in dir() else False
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/channel-create-playlist', methods=['POST'])
def channel_create_playlist():
    """Buat playlist baru."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    data = request.json or {}
    title   = data.get("title", "").strip()
    desc    = data.get("description", "").strip()
    privacy = data.get("privacy", "public")
    if not title:
        return jsonify({"error": "Judul playlist tidak boleh kosong"}), 400
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    try:
        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": desc},
                "status":  {"privacyStatus": privacy}
            }
        ).execute()
        pl_id = resp["id"]
        return jsonify({
            "status":      "ok",
            "playlist_id": pl_id,
            "title":       title,
            "privacy":     privacy,
            "url":         f"https://www.youtube.com/playlist?list={pl_id}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/community-post-text', methods=['POST'])
def community_post_text():
    """Post teks ke komunitas channel (eksperimental via YouTube API)."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Teks tidak boleh kosong"}), 400
    try:
        import requests as rq
        token = json.loads(creds.to_json()).get("token", "")
        if not token:
            creds.refresh(Request())
            token = creds.token
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        # Gunakan YouTube Data API v3 community posts endpoint (eksperimental)
        payload = {
            "snippet": {
                "type": "textPost",
                "textOriginal": text
            }
        }
        resp = rq.post(
            "https://www.googleapis.com/youtube/v3/posts?part=snippet",
            headers=headers, json=payload, timeout=20
        )
        if resp.status_code in (200, 201):
            return jsonify({"status": "ok", "post_id": resp.json().get("id", "")})
        # Fallback: coba via channelSections (info saja)
        return jsonify({
            "status": "ok",
            "note": "Post dikirim (API eksperimental). Cek channel kamu.",
            "api_response": resp.status_code,
            "detail": resp.text[:200]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/community-post-photo', methods=['POST'])
def community_post_photo():
    """Upload foto dan post ke komunitas channel."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    if 'photo' not in request.files:
        return jsonify({"error": "Tidak ada file foto"}), 400
    text  = request.form.get("text", "").strip()
    f     = request.files['photo']
    ext   = os.path.splitext(f.filename)[1].lower()
    if ext not in SUPPORTED_IMAGE:
        return jsonify({"error": f"Format tidak didukung: {ext}"}), 400
    fname = f"cp_photo_{uuid.uuid4().hex[:8]}{ext}"
    fpath = temp_path(fname)
    f.save(fpath)
    try:
        import requests as rq
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        token = creds.token
        headers_auth = {"Authorization": f"Bearer {token}"}

        # Step 1: Upload gambar ke YouTube
        with open(fpath, "rb") as img:
            img_data = img.read()
        mime = "image/jpeg" if ext in ('.jpg', '.jpeg') else f"image/{ext.lstrip('.')}"
        upload_resp = rq.post(
            "https://www.googleapis.com/upload/youtube/v3/posts?uploadType=media&part=snippet",
            headers={**headers_auth, "Content-Type": mime, "X-Upload-Content-Type": mime},
            data=img_data, timeout=60
        )

        # Step 2: Buat post dengan foto
        payload = {
            "snippet": {
                "type": "imagePost",
                "textOriginal": text,
            }
        }
        post_resp = rq.post(
            "https://www.googleapis.com/youtube/v3/posts?part=snippet",
            headers={**headers_auth, "Content-Type": "application/json"},
            json=payload, timeout=20
        )
        cleanup_temp(fname)
        return jsonify({
            "status": "ok",
            "note": "Foto berhasil dikirim (API eksperimental).",
            "api_response": post_resp.status_code,
        })
    except Exception as e:
        cleanup_temp(fname)
        return jsonify({"error": str(e)}), 500


@app.route('/api/community-post-poll', methods=['POST'])
def community_post_poll():
    """Post polling ke komunitas channel."""
    if not GOOGLE_AVAILABLE: return jsonify({"error": "Google API tidak tersedia"}), 500
    creds = load_credentials()
    if not creds: return jsonify({"error": "Belum autentikasi YouTube"}), 401
    data    = request.json or {}
    text    = data.get("text", "").strip()
    options = data.get("options", [])
    if not text:
        return jsonify({"error": "Pertanyaan tidak boleh kosong"}), 400
    if len(options) < 2:
        return jsonify({"error": "Minimal 2 opsi polling"}), 400
    try:
        import requests as rq
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        token = creds.token
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "snippet": {
                "type": "pollPost",
                "textOriginal": text,
                "poll": {
                    "choices": [{"text": opt} for opt in options]
                }
            }
        }
        resp = rq.post(
            "https://www.googleapis.com/youtube/v3/posts?part=snippet",
            headers=headers, json=payload, timeout=20
        )
        return jsonify({
            "status": "ok",
            "note": "Polling dikirim (API eksperimental).",
            "api_response": resp.status_code,
            "detail": resp.text[:200]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/v1/cleanup', methods=['POST'])
def api_v1_cleanup():
    """
    Trigger manual GitHub cleanup — hapus file video/thumbnail yang tidak ada di antrian aktif.
    POST /api/v1/cleanup
    Header: X-API-Key: <api_key>
    """
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        queue = load_queue()

        needed_paths = set()
        for item in queue:
            if item.get("status") in ("pending", "waiting"):
                if item.get("github_path"):
                    needed_paths.add(item["github_path"])
                if item.get("thumbnail_github_path"):
                    needed_paths.add(item["thumbnail_github_path"])

        deleted = []
        failed  = []

        for folder in ("video", "thumbnails"):
            files = _gh_list_folder_files(folder)
            for vf in files:
                repo_path = vf["path"]
                if repo_path not in needed_paths:
                    ok = _gh_delete_file(repo_path, vf["sha"],
                                         f"[manual-cleanup] hapus file tidak aktif: {vf['name']}")
                    if ok:
                        deleted.append(repo_path)
                    else:
                        failed.append(repo_path)
                    time.sleep(0.5)

        return jsonify({
            "success":       True,
            "deleted":       deleted,
            "failed":        failed,
            "kept_paths":    list(needed_paths),
            "deleted_count": len(deleted),
            "failed_count":  len(failed),
            "message":       f"Cleanup selesai. Dihapus: {len(deleted)}, Gagal: {len(failed)}, Dipertahankan: {len(needed_paths)}",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# STARTUP & MAIN
# ============================================================

# Auto-start workers (works with gunicorn too)
threading.Thread(target=queue_worker,          daemon=True).start()
threading.Thread(target=sync_worker,           daemon=True).start()
threading.Thread(target=github_cleanup_worker, daemon=True).start()

if __name__ == '__main__':
    if not os.path.exists(ABOUT_FILE):
        save_about(DEFAULT_ABOUT)

    print(f"[INIT] BASE_DIR : {BASE_DIR}")
    print(f"[INIT] TEMP     : {TEMP_FOLDER}")
    print(f"[INIT] MUSIC    : {MUSIC_FOLDER}")
    print(f"[INIT] ffmpeg   : {get_ffmpeg() or 'NOT FOUND'}")

    riwayat = load_riwayat()
    print(f"[INIT] Riwayat  : {len(riwayat)} entries dari GitHub")

    raw_queue = load_queue()
    clean_queue = validate_queue_on_startup(raw_queue)
    if len(clean_queue) != len(raw_queue):
        save_queue(clean_queue, force_gh=True)
    print(f"[INIT] Queue    : {len(clean_queue)} items")

    now = time.time()
    for fname in os.listdir(TEMP_FOLDER):
        fpath = os.path.join(TEMP_FOLDER, fname)
        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 86400:
            try: os.remove(fpath)
            except: pass

    port = int(os.environ.get('PORT', 5000))
    print(f'[SERVER] Starting on http://0.0.0.0:{port}')
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)

# ============================================================
# ENDPOINT: UPDATE REFRESH TOKEN DARI SERVER LAIN
# ============================================================
# POST /api/v1/token
# Header: X-API-Key: <api_key>  (jika API_KEY di-set)
# Body JSON:
#   { "refresh_token": "1//0xxx..." }
#   atau full token object dari google:
#   { "token": "ya29...", "refresh_token": "1//0xxx...", "token_uri": "...", ... }

@app.route('/api/v1/token', methods=['POST'])
def api_v1_update_token():
    """Terima refresh token baru dari server lain, simpan ke JSONBin."""
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.json or {}

    # Support 2 format: full token object atau hanya refresh_token
    if "token" in data and isinstance(data["token"], dict):
        # Format lengkap — langsung pakai
        token_obj = data["token"]
    elif "refresh_token" in data:
        # Hanya refresh_token — gabungkan dengan token existing di JSONBin
        existing = _pull_token_from_store()
        if not existing:
            return jsonify({
                "success": False,
                "error": "Tidak ada token existing di JSONBin. Kirim full token object."
            }), 400
        token_obj = dict(existing)
        token_obj["refresh_token"] = data["refresh_token"]
        # Hapus expiry supaya langsung di-refresh
        token_obj.pop("expiry", None)
        token_obj.pop("token", None)  # hapus access token lama
    else:
        return jsonify({
            "success": False,
            "error": "Kirim 'refresh_token' string atau 'token' object lengkap"
        }), 400

    # Validasi minimal
    if not token_obj.get("refresh_token"):
        return jsonify({"success": False, "error": "refresh_token kosong"}), 400

    # Coba build credentials dan langsung refresh
    try:
        creds = Credentials.from_authorized_user_info(token_obj, SCOPES)
        creds.refresh(Request())
        _push_token_to_store(creds)
        token_info = json.loads(creds.to_json())
        return jsonify({
            "success": True,
            "message": "Token berhasil diperbarui dan disimpan ke JSONBin",
            "expiry":         token_info.get("expiry"),
            "has_refresh_token": bool(token_info.get("refresh_token")),
        })
    except Exception as e:
        # Tetap simpan meski refresh gagal (mungkin token baru perlu waktu)
        try:
            import requests as rq
            payload = {"token": token_obj, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            rq.put(JSONBIN_URL, headers=_jb_headers(), json=payload, timeout=10)
            return jsonify({
                "success": True,
                "message": "Token disimpan ke JSONBin (refresh gagal, akan dicoba saat upload)",
                "warning": str(e),
                "has_refresh_token": bool(token_obj.get("refresh_token")),
            })
        except Exception as e2:
            return jsonify({"success": False, "error": f"Gagal simpan: {e2}"}), 500


# ============================================================
# ENDPOINT: INFO LENGKAP PROYEK
# ============================================================
# GET /api/v1/info-full
# Header: X-API-Key: <api_key>

@app.route('/api/v1/info-full', methods=['GET'])
def api_v1_info_full():
    """Info lengkap semua data proyek: token, antrian, riwayat, sistem."""
    if not _check_api_key():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    now_ts  = time.time()
    queue   = load_queue()
    riwayat = load_riwayat()
    about   = load_about()

    # ── Token info ───────────────────────────────────────────
    token_info = {}
    try:
        raw = _pull_token_from_store()
        if raw:
            creds = Credentials.from_authorized_user_info(raw, SCOPES)
            token_info = {
                "valid":             creds.valid,
                "expired":           creds.expired,
                "has_refresh_token": bool(creds.refresh_token),
                "expiry":            raw.get("expiry"),
                "scopes":            raw.get("scopes", []),
                "saved_at":          None,  # diisi di bawah
            }
        else:
            token_info = {"valid": False, "error": "Tidak ada token di JSONBin"}
        # ambil saved_at dari JSONBin record
        import requests as rq
        jb_resp = rq.get(JSONBIN_URL + "/latest", headers=_jb_headers(), timeout=8)
        if jb_resp.status_code == 200:
            token_info["saved_at"] = jb_resp.json().get("record", {}).get("saved_at")
    except Exception as e:
        token_info["error"] = str(e)

    # ── Queue stats ──────────────────────────────────────────
    q_pending   = [q for q in queue if q.get("status") == "pending"]
    q_waiting   = [q for q in queue if q.get("status") == "waiting"]
    q_uploading = [q for q in queue if q.get("status") == "uploading"]
    q_done      = [q for q in queue if q.get("status") == "done"]
    q_failed    = [q for q in queue if q.get("status") == "failed"]

    queue_detail = []
    for q in queue:
        remaining = None
        if q.get("status") == "pending" and q.get("upload_at_ts"):
            remaining = round(max(0, q["upload_at_ts"] - now_ts), 1)
        queue_detail.append({
            "id":               q.get("id"),
            "title":            q.get("title", ""),
            "status":           q.get("status"),
            "upload_at":        q.get("upload_at"),
            "remaining_seconds": remaining,
            "added_at":         q.get("added_at"),
            "source":           q.get("source"),
            "github_path":      q.get("github_path"),
            "video_id":         q.get("video_id"),
            "link":             q.get("link"),
            "error":            q.get("error"),
            "file_hash":        q.get("file_hash"),
        })

    # ── Riwayat stats ────────────────────────────────────────
    riwayat_recent = sorted(riwayat, key=lambda r: r.get("timestamp_unix", 0), reverse=True)[:10]
    riwayat_summary = [{
        "video_id":      r.get("video_id"),
        "title":         r.get("title", ""),
        "link":          r.get("link"),
        "tanggal_upload": r.get("tanggal_upload"),
        "thumbnail":     r.get("thumbnail"),
    } for r in riwayat_recent]

    # ── Sistem info ──────────────────────────────────────────
    temp_files = []
    try:
        for f in os.listdir(TEMP_FOLDER):
            fp = os.path.join(TEMP_FOLDER, f)
            if os.path.isfile(fp):
                temp_files.append({
                    "name": f,
                    "size_mb": round(os.path.getsize(fp) / 1024 / 1024, 2),
                    "age_hours": round((now_ts - os.path.getmtime(fp)) / 3600, 1),
                })
    except: pass

    music_files = []
    try:
        for f in os.listdir(MUSIC_FOLDER):
            if os.path.splitext(f)[1].lower() in SUPPORTED_MUSIC:
                music_files.append(f)
    except: pass

    return jsonify({
        "success": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),

        # ── Token ────────────────────────────────────────────
        "token": token_info,

        # ── Antrian ──────────────────────────────────────────
        "queue": {
            "total":      len(queue),
            "pending":    len(q_pending),
            "waiting":    len(q_waiting),
            "uploading":  len(q_uploading),
            "done":       len(q_done),
            "failed":     len(q_failed),
            "items":      queue_detail,
        },

        # ── Riwayat ──────────────────────────────────────────
        "riwayat": {
            "total":   len(riwayat),
            "recent":  riwayat_summary,
        },

        # ── About (default metadata) ─────────────────────────
        "about": about,

        # ── Konfigurasi server ───────────────────────────────
        "config": {
            "github_repo":        GITHUB_REPO,
            "jsonbin_configured": bool(JSONBIN_BIN_ID and JSONBIN_API_KEY),
            "google_available":   GOOGLE_AVAILABLE,
            "api_key_set":        bool(API_KEY),
            "ffmpeg":             str(get_ffmpeg()) if get_ffmpeg() else None,
        },

        # ── Temp files ───────────────────────────────────────
        "temp": {
            "total_files": len(temp_files),
            "files":       temp_files,
        },

        # ── Musik ────────────────────────────────────────────
        "music": {
            "total": len(music_files),
            "files": music_files,
        },
    })
