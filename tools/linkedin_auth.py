"""Отримати токен LinkedIn (діє 60 днів) і свій URN автора.

Запуск на своєму комп'ютері:
    python tools/linkedin_auth.py CLIENT_ID CLIENT_SECRET

У налаштуваннях застосунку LinkedIn (вкладка Auth) додай Redirect URL:
    http://localhost:8765/callback
Потрібні продукти застосунку: "Sign In with LinkedIn using OpenID Connect" і "Share on LinkedIn".
"""
import http.server
import secrets
import sys
import urllib.parse
import webbrowser

import requests

REDIRECT = "http://localhost:8765/callback"
SCOPES = "openid profile w_member_social"


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово, можна закрити вкладку.".encode())

        def log_message(self, *a):
            pass

    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "state": state, "scope": SCOPES,
    })
    print("Відкриваю браузер для входу в LinkedIn...\n", url)
    webbrowser.open(url)
    http.server.HTTPServer(("localhost", 8765), Handler).handle_request()

    if got.get("state") != state or "code" not in got:
        sys.exit(f"Помилка авторизації: {got}")
    tok = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
        "client_id": client_id, "client_secret": client_secret,
    }, timeout=30).json()
    if "access_token" not in tok:
        sys.exit(f"Помилка токена: {tok}")
    me = requests.get("https://api.linkedin.com/v2/userinfo",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30).json()
    print("\nДодай у GitHub → Settings → Secrets and variables → Actions:")
    print(f"  LINKEDIN_ACCESS_TOKEN = {tok['access_token']}")
    print(f"  LINKEDIN_AUTHOR_URN   = urn:li:person:{me['sub']}")
    print(f"\nТокен діє {int(tok.get('expires_in', 0)) // 86400} днів — постав собі нагадування оновити.")


if __name__ == "__main__":
    main()
