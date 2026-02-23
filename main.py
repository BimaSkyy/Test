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

def load_credentials():
    if not GOOGLE_AVAILABLE: return None
    try:
        # Prioritas 1: baca dari environment variable (untuk Koyeb/cloud)
        token_json_str = os.environ.get("YOUTUBE_TOKEN_JSON", "")
        if token_json_str:
            token_data = json.loads(token_json_str)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            if creds and creds.valid: return creds
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                print("[AUTH] Token refreshed dari env var")
                return creds

        # Prioritas 2: baca dari file lokal (untuk development)
        token_path = os.path.join(BASE_DIR, "token.json")
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            if creds and creds.valid: return creds
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                return creds
    except Exception as e:
        print(f"[AUTH] error: {e}")
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

def do_youtube_upload(file_path, title, description, tags, category, file_hash, filename, playlist_id=None):
    if not GOOGLE_AVAILABLE: return None, "Google API tidak tersedia"
    creds = load_credentials()
    if not creds: return None, "Belum autentikasi YouTube"
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
        vid_id, err = do_youtube_upload(
            file_path,
            it.get("title", about.get("title", "")),
            it.get("description", about.get("description", "")),
            it.get("tags", about.get("tags", [])),
            it.get("category", about.get("category", "20")),
            it.get("file_hash", ""), filename,
            playlist_id=it.get("playlist_id", about.get("playlist", ""))
        )

        if vid_id and github_path:
            print(f"[QUEUE] Berhasil upload YouTube, hapus dari GitHub: {github_path}")
            gh_delete_video(github_path)

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
            with queue_lock:
                queue = load_queue()
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
                            save_queue(queue, force_gh=True)
                            item_copy = dict(item)
                            threading.Thread(target=do_upload, args=(item_copy,), daemon=True).start()
                        else:
                            save_queue(queue)
        except Exception as e:
            print(f"[QUEUE WORKER] Error: {e}")
        time.sleep(1)

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
# STARTUP & MAIN
# ============================================================

# Auto-start workers (works with gunicorn too)
threading.Thread(target=queue_worker, daemon=True).start()
threading.Thread(target=sync_worker,  daemon=True).start()

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

    threading.Thread(target=queue_worker, daemon=True).start()
    threading.Thread(target=sync_worker,  daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    print(f'[SERVER] Starting on http://0.0.0.0:{port}')
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
