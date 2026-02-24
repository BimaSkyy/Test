"""
Script untuk setup OAuth token pertama kali.
Jalankan sekali: python auth_setup.py

Setelah ini token akan otomatis tersimpan di GitHub
dan di-refresh sendiri tanpa perlu ganti manual lagi.
"""
import os
import json
import base64
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload"
]

# ============================================================
# GitHub config — isi sesuai environment variable kamu
# ============================================================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")  # contoh: "BimaSkyy/myhistory"
GITHUB_API   = "https://api.github.com"

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
    }

def save_token_to_github(token_str):
    """Simpan token JSON ke GitHub repo."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("⚠️  GITHUB_TOKEN atau GITHUB_REPO belum di-set, skip simpan ke GitHub.")
        return False
    try:
        import requests as r

        # Cek apakah file sudah ada (untuk ambil sha)
        sha = None
        resp_get = r.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/data/youtube_token.json",
            headers=_gh_headers(), timeout=15
        )
        if resp_get.status_code == 200:
            sha = resp_get.json().get("sha")

        encoded = base64.b64encode(token_str.encode("utf-8")).decode("utf-8")
        payload = {
            "message": "[auth] save youtube token",
            "content": encoded
        }
        if sha:
            payload["sha"] = sha

        resp = r.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/data/youtube_token.json",
            headers=_gh_headers(), json=payload, timeout=20
        )
        if resp.status_code in (200, 201):
            print("✅ Token berhasil disimpan ke GitHub!")
            return True
        else:
            print(f"❌ Gagal simpan ke GitHub: {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Error simpan ke GitHub: {e}")
        return False

def setup_auth():
    creds = None

    # Cek token lokal dulu
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("✅ Token diperbarui!")
        else:
            if not os.path.exists("credentials.json"):
                print("❌ credentials.json tidak ditemukan!")
                print("📌 Download dari Google Cloud Console dan taruh di folder ini.")
                return

            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob"
            )
            auth_url, _ = flow.authorization_url(
                prompt='consent',
                access_type='offline'  # Pastikan dapat refresh_token
            )
            print("\n" + "="*60)
            print("📢 Buka link berikut di browser:")
            print(f"\n🔗 {auth_url}\n")
            print("="*60)
            code = input("\n💬 Paste kode verifikasi dari browser: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials

        # Simpan ke file lokal
        token_str = creds.to_json()
        with open("token.json", "w") as f:
            f.write(token_str)
        print("\n✅ token.json berhasil dibuat!")

        # Simpan ke GitHub supaya Koyeb bisa auto-refresh
        print("\n🔄 Menyimpan token ke GitHub repo...")
        saved = save_token_to_github(token_str)

        if saved:
            print("\n🎉 Setup selesai! Token akan auto-refresh sendiri.")
            print("   Kamu tidak perlu ganti token lagi selama refresh_token masih aktif.")
        else:
            print("\n⚠️  Token tersimpan lokal tapi gagal ke GitHub.")
            print("   Copy isi token.json ke env var YOUTUBE_TOKEN_JSON di Koyeb.")

        print("\n▶  Jalankan: python main.py")

    else:
        print("✅ Token sudah valid!")
        token_str = creds.to_json()

        # Pastikan juga tersimpan di GitHub
        print("🔄 Sinkronisasi token ke GitHub...")
        save_token_to_github(token_str)
        print("▶  Jalankan: python main.py")

    # Print ringkasan token (tanpa secret)
    try:
        token_data = json.loads(creds.to_json())
        has_refresh = bool(token_data.get("refresh_token"))
        print(f"\n📋 Info token:")
        print(f"   refresh_token : {'✅ Ada' if has_refresh else '❌ Tidak ada!'}")
        print(f"   expiry        : {token_data.get('expiry', 'unknown')}")
        if not has_refresh:
            print("\n⚠️  PERINGATAN: refresh_token tidak ada!")
            print("   Token akan expired dan tidak bisa auto-refresh.")
            print("   Solusi: hapus token.json dan jalankan auth_setup.py lagi.")
    except:
        pass

if __name__ == "__main__":
    setup_auth()
