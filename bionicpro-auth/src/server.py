import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
KEYCLOAK_EXTERNAL_URL = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
KEYCLOAK_INTERNAL_URL = os.getenv("KEYCLOAK_INTERNAL_URL", KEYCLOAK_EXTERNAL_URL)
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "reports-realm")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "bionicpro-auth")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "bionicpro-auth-secret")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_NAME = os.getenv("COOKIE_NAME", "bionicpro_session")
PROFILE_DB_PATH = os.getenv("PROFILE_DB_PATH", "/data/yandex_profiles.sqlite3")
REPORTS_API_URL = os.getenv("REPORTS_API_URL", "http://reports-api:8010")

sessions: dict[str, dict] = {}
pending_auth: dict[str, dict] = {}
lock = threading.RLock()
db_lock = threading.RLock()


def realm_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/realms/{KEYCLOAK_REALM}"


def token_endpoint() -> str:
    return f"{realm_url(KEYCLOAK_INTERNAL_URL)}/protocol/openid-connect/token"


def userinfo_endpoint() -> str:
    return f"{realm_url(KEYCLOAK_INTERNAL_URL)}/protocol/openid-connect/userinfo"


def auth_endpoint() -> str:
    return f"{realm_url(KEYCLOAK_EXTERNAL_URL)}/protocol/openid-connect/auth"


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL.rstrip('/')}/auth/callback"


def now() -> int:
    return int(time.time())


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def form_post(url: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def bearer_get(url: str, access_token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def json_get(url: str, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def init_profile_db() -> None:
    db_dir = os.path.dirname(PROFILE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with db_lock, sqlite3.connect(PROFILE_DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS external_profiles (
                subject TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                username TEXT,
                email TEXT,
                full_name TEXT,
                given_name TEXT,
                family_name TEXT,
                raw_profile TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )


def save_external_profile(profile: dict, provider: str = "yandex") -> None:
    subject = profile.get("sub") or profile.get("id") or profile.get("preferred_username")
    if not subject:
        return
    username = profile.get("preferred_username") or profile.get("login")
    email = profile.get("email") or profile.get("default_email")
    full_name = profile.get("name") or profile.get("real_name") or profile.get("display_name")
    given_name = profile.get("given_name") or profile.get("first_name")
    family_name = profile.get("family_name") or profile.get("last_name")
    with db_lock, sqlite3.connect(PROFILE_DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO external_profiles (
                subject, provider, username, email, full_name, given_name,
                family_name, raw_profile, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                provider = excluded.provider,
                username = excluded.username,
                email = excluded.email,
                full_name = excluded.full_name,
                given_name = excluded.given_name,
                family_name = excluded.family_name,
                raw_profile = excluded.raw_profile,
                updated_at = excluded.updated_at
            """,
            (
                subject,
                provider,
                username,
                email,
                full_name,
                given_name,
                family_name,
                json.dumps(profile, ensure_ascii=False, sort_keys=True),
                now(),
            ),
        )


def cookie_value(headers) -> str | None:
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return None


def make_cookie(session_id: str, max_age: int = SESSION_TTL_SECONDS) -> str:
    secure = "; Secure" if COOKIE_SECURE else ""
    return (
        f"{COOKIE_NAME}={session_id}; Path=/; Max-Age={max_age}; "
        f"HttpOnly{secure}; SameSite=Lax"
    )


def clear_cookie() -> str:
    secure = "; Secure" if COOKIE_SECURE else ""
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly{secure}; SameSite=Lax"


def prune_expired() -> None:
    cutoff = now()
    with lock:
        for session_id in list(sessions):
            if sessions[session_id]["session_expires_at"] <= cutoff:
                sessions.pop(session_id, None)
        for state in list(pending_auth):
            if pending_auth[state]["expires_at"] <= cutoff:
                pending_auth.pop(state, None)


def create_session(token_response: dict) -> tuple[str, dict]:
    session_id = secrets.token_urlsafe(48)
    current = now()
    session = {
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "access_expires_at": current + int(token_response.get("expires_in", 120)) - 5,
        "session_expires_at": current + SESSION_TTL_SECONDS,
    }
    with lock:
        sessions[session_id] = session
    return session_id, session


def refresh_session(session: dict) -> None:
    refreshed = form_post(
        token_endpoint(),
        {
            "grant_type": "refresh_token",
            "client_id": KEYCLOAK_CLIENT_ID,
            "client_secret": KEYCLOAK_CLIENT_SECRET,
            "refresh_token": session["refresh_token"],
        },
    )
    session["access_token"] = refreshed["access_token"]
    session["refresh_token"] = refreshed.get("refresh_token", session["refresh_token"])
    session["access_expires_at"] = now() + int(refreshed.get("expires_in", 120)) - 5


def rotate_session(session_id: str, session: dict) -> str:
    new_session_id = secrets.token_urlsafe(48)
    with lock:
        sessions.pop(session_id, None)
        sessions[new_session_id] = session
    return new_session_id


class Handler(BaseHTTPRequestHandler):
    server_version = "bionicpro-auth/1.0"

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", FRONTEND_ORIGIN)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        prune_expired()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.json_response({"status": "ok"})
        elif parsed.path == "/auth/login":
            self.start_login()
        elif parsed.path == "/auth/callback":
            self.finish_login(parsed)
        elif parsed.path == "/auth/session":
            self.session_info()
        elif parsed.path == "/api/reports":
            self.reports()
        else:
            self.json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        prune_expired()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/auth/logout":
            self.logout()
        else:
            self.json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def start_login(self):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        with lock:
            pending_auth[state] = {"verifier": verifier, "expires_at": now() + 300}

        params = {
            "client_id": KEYCLOAK_CLIENT_ID,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", f"{auth_endpoint()}?{urllib.parse.urlencode(params)}")
        self.end_headers()

    def finish_login(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        with lock:
            auth = pending_auth.pop(state, None)

        if not auth or not code:
            self.redirect_frontend("/?auth=failed")
            return

        try:
            token_response = form_post(
                token_endpoint(),
                {
                    "grant_type": "authorization_code",
                    "client_id": KEYCLOAK_CLIENT_ID,
                    "client_secret": KEYCLOAK_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri(),
                    "code_verifier": auth["verifier"],
                },
            )
            profile = bearer_get(userinfo_endpoint(), token_response["access_token"])
            save_external_profile(profile)
            session_id, _ = create_session(token_response)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, sqlite3.Error):
            self.redirect_frontend("/?auth=failed")
            return

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Set-Cookie", make_cookie(session_id))
        self.send_header("Location", FRONTEND_ORIGIN)
        self.end_headers()

    def get_session(self) -> tuple[str | None, dict | None]:
        session_id = cookie_value(self.headers)
        if not session_id:
            return None, None
        with lock:
            session = sessions.get(session_id)
        if not session or session["session_expires_at"] <= now():
            with lock:
                sessions.pop(session_id, None)
            return None, None
        if session["access_expires_at"] <= now():
            refresh_session(session)
        return session_id, session

    def session_info(self):
        try:
            session_id, session = self.get_session()
            if not session_id or not session:
                self.json_response({"authenticated": False}, HTTPStatus.UNAUTHORIZED)
                return
            profile = bearer_get(userinfo_endpoint(), session["access_token"])
            save_external_profile(profile)
            self.json_response(
                {
                    "authenticated": True,
                    "username": profile.get("preferred_username"),
                    "email": profile.get("email"),
                    "expiresAt": session["session_expires_at"],
                }
            )
        except (urllib.error.URLError, json.JSONDecodeError):
            self.json_response({"authenticated": False}, HTTPStatus.UNAUTHORIZED)

    def reports(self):
        try:
            session_id, session = self.get_session()
            if not session_id or not session:
                self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return

            profile = bearer_get(userinfo_endpoint(), session["access_token"])
            user_id = profile.get("preferred_username") or profile.get("sub")
            if not user_id:
                self.json_response({"error": "user_not_found"}, HTTPStatus.UNAUTHORIZED)
                return

            reports_url = (
                f"{REPORTS_API_URL.rstrip('/')}/reports?"
                f"{urllib.parse.urlencode({'user_id': user_id})}"
            )
            report_response = json_get(reports_url, headers={"X-User-Id": user_id})
            new_session_id = rotate_session(session_id, session)
            self.json_response(
                report_response,
                extra_headers={"Set-Cookie": make_cookie(new_session_id)},
            )
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except json.JSONDecodeError:
                payload = {"error": "reports_api_error"}
            self.json_response(payload, HTTPStatus(error.code))
        except (urllib.error.URLError, json.JSONDecodeError):
            self.json_response({"error": "reports_api_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)

    def logout(self):
        session_id = cookie_value(self.headers)
        if session_id:
            with lock:
                sessions.pop(session_id, None)
        self.json_response({"ok": True}, extra_headers={"Set-Cookie": clear_cookie()})

    def redirect_frontend(self, path: str):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", f"{FRONTEND_ORIGIN.rstrip('/')}{path}")
        self.end_headers()

    def json_response(self, payload: dict, status: HTTPStatus = HTTPStatus.OK, extra_headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    init_profile_db()
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"bionicpro-auth listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
