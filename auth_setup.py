"""
Script untuk setup OAuth token pertama kali.
Jalankan sekali: python auth_setup.py
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload"
]

def setup_auth():
    creds = None
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
            auth_url, _ = flow.authorization_url(prompt='consent')
            print("\n" + "="*60)
            print("📢 Buka link berikut di browser:")
            print(f"\n🔗 {auth_url}\n")
            print("="*60)
            code = input("\n💬 Paste kode verifikasi dari browser: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials
        
        with open("token.json", "w") as f:
            f.write(creds.to_json())
        print("\n✅ token.json berhasil dibuat! Sekarang jalankan: python app.py")
    else:
        print("✅ Token sudah valid! Jalankan: python app.py")

if __name__ == "__main__":
    setup_auth()
