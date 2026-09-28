from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from functools import wraps
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import psutil
from flask import Flask, Response, jsonify, make_response, redirect, request, send_file, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
USERS_DIR = Path(os.environ.get("USERS_DIR", DATA_DIR / "users")).resolve()
DB_FILE = Path(os.environ.get("DB_FILE", DATA_DIR / "host.db")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
secret = os.environ.get("SECRET_KEY")
if not secret or len(secret) < 32:
    # A random key is safer than a weak committed fallback. Production must set SECRET_KEY.
    secret = secrets.token_hex(32)
app.secret_key = secret
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", 512 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    SESSION_COOKIE_SAMESITE="Lax",
)

BRAND = "•-تــيــ۾ إڪـXـس-•"
VALID_LANGUAGES = {"python", "php", "node", "javascript", "c", "cpp", "html"}
SERVER_STATES = {"CREATED", "STARTING", "RUNNING", "STOPPING", "STOPPED", "CRASHED", "ERROR", "INSTALLING", "BUILDING"}
processes: dict[int, subprocess.Popen] = {}
process_lock = threading.RLock()
log_conditions: dict[int, threading.Condition] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
              account_type TEXT NOT NULL DEFAULT 'User', role TEXT NOT NULL DEFAULT 'User',
              status TEXT NOT NULL DEFAULT 'ACTIVE', subscription_started_at TEXT,
              subscription_expires_at TEXT, max_servers INTEGER NOT NULL DEFAULT 3,
              storage_per_server INTEGER NOT NULL DEFAULT 1073741824,
              ram_per_server INTEGER NOT NULL DEFAULT 536870912,
              cpu_per_server REAL NOT NULL DEFAULT 1.0, max_backups INTEGER NOT NULL DEFAULT 3,
              api_key_hash TEXT, created_at TEXT NOT NULL, last_login TEXT
            );
            CREATE TABLE IF NOT EXISTS servers (
              id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, language TEXT NOT NULL,
              path TEXT NOT NULL, startup_file TEXT, status TEXT NOT NULL DEFAULT 'CREATED',
              pid INTEGER, port INTEGER, start_time REAL, exit_code INTEGER,
              auto_restart INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_logs (
              id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
              action TEXT NOT NULL, target TEXT, details TEXT, ip TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backups (
              id INTEGER PRIMARY KEY, server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
              filename TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broadcasts (
              id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
              message TEXT NOT NULL, audience TEXT NOT NULL, targeted INTEGER NOT NULL DEFAULT 0,
              successful INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_servers_owner ON servers(owner_id);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON activity_logs(created_at);
            """
        )
        # Additive migrations: preserve all existing tables and data while introducing
        # account quotas, explicit server types, and website metadata.
        migrations = {
            "users": {"storage_limit": "INTEGER NOT NULL DEFAULT 1073741824", "storage_used": "INTEGER NOT NULL DEFAULT 0", "account_expiry": "TEXT"},
            "servers": {"server_type": "TEXT NOT NULL DEFAULT 'bot'", "server_slug": "TEXT", "server_language": "TEXT", "website_url": "TEXT", "last_activity": "TEXT"},
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("UPDATE users SET storage_limit=storage_per_server WHERE storage_limit IS NULL OR storage_limit=0")
        conn.execute("UPDATE users SET account_expiry=subscription_expires_at WHERE account_expiry IS NULL")
        conn.execute("UPDATE servers SET server_type=CASE WHEN lower(language)='html' THEN 'website' ELSE 'bot' END WHERE server_type IS NULL OR server_type=''")
        conn.execute("UPDATE servers SET server_slug=slug,server_language=language WHERE server_slug IS NULL OR server_language IS NULL")
        conn.execute("UPDATE servers SET last_activity=updated_at WHERE last_activity IS NULL")
        # Archives may contain absolute paths from another checkout; re-anchor them safely.
        for row in conn.execute("SELECT id,owner_id,path,slug FROM servers").fetchall():
            old = Path(row[2]) if row[2] else Path()
            target = USERS_DIR / f"user_{row[1]}" / "servers" / f"{row[0]}_{row[3]}"
            if old != target and (not old.exists() or str(old).startswith(str(BASE_DIR)) is False):
                target.mkdir(parents=True, exist_ok=True)
                conn.execute("UPDATE servers SET path=? WHERE id=?", (str(target), row[0]))

        # Requested built-in credentials. Prefer ADMIN_USERNAME/ADMIN_PASSWORD in production.
        admin_user = os.environ.get("ADMIN_USERNAME", "Admin@gmail.com").strip().lower()
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            admin_password = secrets.token_urlsafe(18) + "A1"
            print(f"Generated initial admin password (set ADMIN_PASSWORD to replace it): {admin_password}")
        exists = conn.execute("SELECT id FROM users WHERE username=?", (admin_user,)).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO users(username,password_hash,account_type,role,status,subscription_started_at,
                   subscription_expires_at,max_servers,storage_per_server,ram_per_server,cpu_per_server,max_backups,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (admin_user, generate_password_hash(admin_password, method="scrypt"), "Administrator", "Owner", "ACTIVE",
                 now_iso(), (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(), 10000,
                 150 * 1024**3, 16 * 1024**3, 16.0, 100, now_iso()),
            )

init_db()


def rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def current_user() -> dict[str, Any] | None:
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as conn:
        return rowdict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def subscription_active(user: dict[str, Any]) -> bool:
    if user["status"] != "ACTIVE":
        return False
    expiry = user.get("subscription_expires_at")
    if not expiry:
        return False
    return datetime.fromisoformat(expiry).timestamp() > time.time()


def permissions(user: dict[str, Any]) -> set[str]:
    role = user.get("role")
    return {
        "Owner": {"users", "servers", "plans", "security"},
        "Administrator": {"users", "servers", "plans"},
        "Manager": {"users", "servers"},
        "Support": {"servers"},
    }.get(role, set())


def require_user(admin_permission: str | None = None):
    user = current_user()
    if not user:
        return None, (jsonify(success=False, message="Authentication required"), 401)
    if admin_permission and (user["account_type"] != "Administrator" or admin_permission not in permissions(user)):
        return None, (jsonify(success=False, message="Permission denied"), 403)
    return user, None


def audit(actor_id: int | None, action: str, target: str = "", details: Any = None) -> None:
    with db() as conn:
        conn.execute("INSERT INTO activity_logs(actor_id,action,target,details,ip,created_at) VALUES(?,?,?,?,?,?)",
                     (actor_id, action, target, json.dumps(details, ensure_ascii=False) if details is not None else None,
                      request.remote_addr if request else None, now_iso()))


def safe_child(base: Path, relative: str = "") -> Path:
    base = base.resolve()
    relative = str(relative or "").replace("\\", "/")
    if relative.startswith("/") or any(part in {"..", ""} and part == ".." for part in Path(relative).parts):
        raise ValueError("Unsafe path")
    candidate = (base / relative).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("Path escapes server directory")
    return candidate


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), now_iso()))


def server_for(user: dict[str, Any], server_id: int) -> dict[str, Any] | None:
    with db() as conn:
        srv = rowdict(conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone())
    if not srv or (srv["owner_id"] != user["id"] and user["account_type"] != "Administrator"):
        return None
    return srv


def json_body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def user_storage(user_id: int, refresh: bool = False) -> int:
    with db() as conn:
        rows = conn.execute("SELECT path FROM servers WHERE owner_id=?", (user_id,)).fetchall()
    return sum(used_bytes(Path(r[0]), refresh=refresh) for r in rows if r[0] and Path(r[0]).exists())


def storage_limit_for(user: dict[str, Any]) -> int:
    return int(user.get("storage_limit") or user.get("storage_per_server") or 0)


def validate_username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,120}", value):
        raise ValueError("Username must contain only safe letters, numbers, dots, @, _ or -")
    return value


def runtime_command(srv: dict[str, Any]) -> list[str] | None:
    path = Path(srv["path"]).resolve()
    lang = srv["language"]
    startup = srv.get("startup_file")
    if startup:
        startup_path = safe_child(path, startup)
        if not startup_path.is_file():
            return None
        startup = str(startup_path.relative_to(path))
    if lang == "python":
        startup = startup or next((x for x in ("main.py", "bot.py", "app.py", "index.py", "run.py") if (path / x).is_file()), None)
        python_bin = path / ".venv" / "bin" / "python"
        interpreter = str(python_bin) if python_bin.is_file() else os.environ.get("PYTHON_BIN", "python3")
        return [interpreter, "-u", startup] if startup else None
    if lang in {"node", "javascript"}:
        if startup:
            return ["node", startup]
        return ["npm", "start"] if (path / "package.json").is_file() else (["node", "index.js"] if (path / "index.js").is_file() else None)
    if lang == "php":
        startup = startup or ("index.php" if (path / "index.php").is_file() else None)
        return ["php", startup] if startup else None
    if lang == "c":
        return [str(path / ".host_build"),] if (path / ".host_build").exists() else None
    if lang == "cpp":
        return [str(path / ".host_build"),] if (path / ".host_build").exists() else None
    if lang == "html":
        return ["python3", "-m", "http.server", str(srv.get("port") or 0)]
    return None


def preexec_for(user: dict[str, Any]):
    def setup():
        os.setsid()
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (int(user["ram_per_server"]), int(user["ram_per_server"])))
            # Thread-heavy libraries such as Telegram clients need more than
            # the old 256-process ceiling; retain isolation without blocking
            # normal worker pools. The host may override this limit.
            nproc_limit = max(512, min(2048, int(os.environ.get("SERVER_NPROC_LIMIT", "1024"))))
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
            cpu = max(1, int(float(user["cpu_per_server"]) * 60))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 10))
        except Exception:
            pass
    return setup


def notify(server_id: int) -> None:
    with log_conditions.setdefault(server_id, threading.Condition()):
        log_conditions[server_id].notify_all()


def append_log(server_id: int, stream: str, text: str) -> None:
    with db() as conn:
        srv = conn.execute("SELECT path FROM servers WHERE id=?", (server_id,)).fetchone()
        if not srv:
            return
        log = Path(srv["path"]) / "console.log"
        with log.open("a", encoding="utf-8", errors="replace") as f:
            for line in text.splitlines() or [""]:
                f.write(json.dumps({"ts": now_iso(), "stream": stream, "line": line}, ensure_ascii=False) + "\n")
    notify(server_id)


def pipe_reader(server_id: int, pipe, stream: str) -> None:
    try:
        for line in iter(pipe.readline, ""):
            append_log(server_id, stream, line)
    finally:
        pipe.close()


def reap_process(server_id: int, proc: subprocess.Popen) -> None:
    code = proc.wait()
    with process_lock:
        processes.pop(server_id, None)
    with db() as conn:
        srv = conn.execute("SELECT auto_restart,status FROM servers WHERE id=?", (server_id,)).fetchone()
        if srv:
            state = "CRASHED" if code else "STOPPED"
            conn.execute("UPDATE servers SET pid=NULL,status=?,exit_code=?,updated_at=? WHERE id=?", (state, code, now_iso(), server_id))
    append_log(server_id, "system", f"Process exited with code {code}")
    notify(server_id)


def install_dependencies_for_server(server_id: int, path: Path, language: str) -> tuple[bool, str]:
    """Install declared dependencies once, inside the server's own directory."""
    if language == "python" and (path / "requirements.txt").is_file():
        venv_python = path / ".venv" / "bin" / "python"
        if venv_python.is_file():
            return True, "Python environment already installed"
        append_log(server_id, "system", "Installing Python dependencies automatically...")
        try:
            created = subprocess.run([os.environ.get("PYTHON_BIN", "python3"), "-m", "venv", ".venv"], cwd=path, capture_output=True, text=True, timeout=300)
            if created.returncode != 0:
                append_log(server_id, "stderr", created.stdout + created.stderr)
                return False, "Could not create Python environment"
            pip = path / ".venv" / "bin" / "pip"
            installed = subprocess.run([str(pip), "install", "--disable-pip-version-check", "-r", "requirements.txt"], cwd=path, capture_output=True, text=True, timeout=600)
            append_log(server_id, "stdout", installed.stdout)
            if installed.returncode != 0:
                append_log(server_id, "stderr", installed.stderr)
                return False, "Dependency installation failed; check console logs"
            append_log(server_id, "system", "Python dependencies installed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            append_log(server_id, "stderr", str(exc))
            return False, "Dependency installation timed out or failed"
    elif language in {"node", "javascript"} and (path / "package.json").is_file() and not (path / "node_modules").is_dir():
        append_log(server_id, "system", "Installing Node.js dependencies automatically...")
        try:
            result = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=path, capture_output=True, text=True, timeout=600)
            append_log(server_id, "stdout", result.stdout)
            if result.returncode != 0:
                append_log(server_id, "stderr", result.stderr)
                return False, "Dependency installation failed; check console logs"
        except (OSError, subprocess.TimeoutExpired) as exc:
            append_log(server_id, "stderr", str(exc))
            return False, "Dependency installation timed out or failed"
    return True, "Dependencies ready"


def start_process(user: dict[str, Any], srv: dict[str, Any]) -> tuple[bool, str]:
    if not subscription_active(user):
        return False, "Subscription is inactive or expired"
    with process_lock:
        old = processes.get(srv["id"])
        if old and old.poll() is None:
            return False, "Server is already running"
    ready, dependency_message = install_dependencies_for_server(srv["id"], Path(srv["path"]), srv["language"])
    if not ready:
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
        return False, dependency_message
    cmd = runtime_command(srv)
    if not cmd or any(not isinstance(x, str) or not x for x in cmd):
        return False, "No valid startup file or build output found"
    path = Path(srv["path"])
    out = path / "console.log"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(path), "PORT": str(srv.get("port") or 0), "SERVER_PORT": str(srv.get("port") or 0)}
    # Never pass host secrets or the full host environment to user workloads.
    env.update({k: v for k, v in os.environ.items() if k.startswith("LANG")})
    try:
        proc = subprocess.Popen(cmd, cwd=path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                bufsize=1, env=env, preexec_fn=preexec_for(user))
    except Exception as exc:
        append_log(srv["id"], "system", f"Failed to start: {exc}")
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
        return False, str(exc)
    with process_lock:
        processes[srv["id"]] = proc
    with db() as conn:
        conn.execute("UPDATE servers SET status=?,pid=?,start_time=?,updated_at=? WHERE id=?", ("RUNNING", proc.pid, time.time(), now_iso(), srv["id"]))
    threading.Thread(target=pipe_reader, args=(srv["id"], proc.stdout, "stdout"), daemon=True).start()
    threading.Thread(target=pipe_reader, args=(srv["id"], proc.stderr, "stderr"), daemon=True).start()
    threading.Thread(target=reap_process, args=(srv["id"], proc), daemon=True).start()
    append_log(srv["id"], "system", f"Started PID {proc.pid}: {' '.join(cmd)}")
    return True, "Server started"


def stop_process(srv: dict[str, Any], kill: bool = False) -> tuple[bool, str]:
    with process_lock:
        proc = processes.get(srv["id"])
    if not proc or proc.poll() is not None:
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,pid=NULL,updated_at=? WHERE id=?", ("STOPPED", now_iso(), srv["id"]))
        return True, "Server is stopped"
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL if kill else signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try: proc.kill()
        except Exception: pass
    with db() as conn:
        conn.execute("UPDATE servers SET status=?,pid=NULL,updated_at=? WHERE id=?", ("STOPPED", now_iso(), srv["id"]))
    append_log(srv["id"], "system", "Killed" if kill else "Stopped")
    return True, "Server stopped"


def allocate_port() -> int:
    with db() as conn:
        used = {r[0] for r in conn.execute("SELECT port FROM servers WHERE port IS NOT NULL")}
    for port in range(8100, 9100):
        if port in used: continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0: return port
    raise RuntimeError("No available ports")


_usage_cache: dict[str, tuple[float, int]] = {}
_usage_cache_lock = threading.Lock()


def used_bytes(path: Path, refresh: bool = False) -> int:
    key = str(path.resolve())
    now = time.time()
    with _usage_cache_lock:
        cached = _usage_cache.get(key)
        if cached and not refresh and now - cached[0] < 5:
            return cached[1]
    total = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    with _usage_cache_lock:
        _usage_cache[key] = (now, total)
    return total


def require_csrf() -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return False
    token = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or json_body().get("csrf_token")
    return bool(token and supplied and secrets.compare_digest(token, supplied))


@app.after_request
def headers(resp):
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "strict-origin-when-cross-origin",
                         "Content-Security-Policy": "default-src 'self' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; connect-src 'self'"})
    return resp


@app.before_request
def guard():
    # Login/register establish the session and therefore cannot depend on a
    # session CSRF token supplied by the browser yet. All other mutations stay protected.
    if request.path.startswith("/api/") and request.path not in {"/api/login", "/api/register"} and not require_csrf():
        return jsonify(success=False, message="CSRF validation failed"), 403
    if get_setting("maintenance", "0") == "1" and request.path.startswith("/api/") and request.path not in {"/api/csrf", "/api/login", "/api/logout", "/api/current_user"}:
        user = current_user()
        if not user or user.get("account_type") != "Administrator":
            return jsonify(success=False, message=get_setting("maintenance_message", "الموقع في وضع الصيانة")), 503


@app.errorhandler(RequestEntityTooLarge)
def too_large(_): return jsonify(success=False, message="Request is too large"), 413

@app.errorhandler(ValueError)
def invalid_request(exc): return jsonify(success=False, message=str(exc)), 400

@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify(success=False, message="Resource not found"), 404
    return "Not found", 404

@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify(success=False, message="Method not allowed"), 405


@app.route("/")
def home():
    if session.get("user_id"): return redirect("/admin" if current_user()["account_type"] == "Administrator" else "/dashboard")
    return redirect("/welcome")


@app.route("/static/team-x-cemetery.png")
def team_x_branding():
    return send_from_directory(BASE_DIR, "team-x-cemetery.png", mimetype="image/png")


@app.route("/welcome")
def welcome(): return send_from_directory(BASE_DIR, "landing.html")


@app.route("/login")
def login_page(): return send_from_directory(BASE_DIR, "login.html")


@app.route("/dashboard")
def dashboard():
    return send_from_directory(BASE_DIR, "index.html") if current_user() else redirect("/login")


@app.route("/admin")
def admin_page():
    user = current_user()
    return send_from_directory(BASE_DIR, "admin_panel.html") if user and user["account_type"] == "Administrator" else redirect("/login")


@app.route("/api/csrf")
def csrf():
    token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return jsonify(token=token)


@app.route("/api/register", methods=["POST"])
def register():
    data = json_body()
    try: username = validate_username(data.get("email") or data.get("username", ""))
    except ValueError as e: return jsonify(success=False, message=str(e)), 400
    password = str(data.get("password", ""))
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return jsonify(success=False, message="Password must be at least 8 characters and include a letter and a number"), 400
    try:
        with db() as conn:
            cur = conn.execute("INSERT INTO users(username,password_hash,account_type,role,status,subscription_started_at,subscription_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               (username, generate_password_hash(password, method="scrypt"), "User", "User", "ACTIVE", now_iso(), (datetime.now(timezone.utc)+timedelta(days=30)).isoformat(), now_iso()))
            uid = cur.lastrowid
            (USERS_DIR / f"user_{uid}" / "servers").mkdir(parents=True, exist_ok=True)
    except sqlite3.IntegrityError: return jsonify(success=False, message="Username already exists"), 409
    audit(uid, "register", username)
    return jsonify(success=True, message="Account created")


@app.route("/api/login", methods=["POST"])
def login():
    data = json_body(); username = str(data.get("email") or data.get("username", "")).strip().lower(); password = str(data.get("password", ""))
    with db() as conn: user = rowdict(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
    if not user or not check_password_hash(user["password_hash"], password):
        audit(user["id"] if user else None, "failed_login", username)
        return jsonify(success=False, message="Invalid credentials"), 401
    if user["status"] != "ACTIVE": return jsonify(success=False, message=f"Account is {user['status'].lower()}"), 403
    session.clear(); session.permanent = True; session["user_id"] = user["id"]; session["csrf_token"] = secrets.token_urlsafe(32)
    with db() as conn: conn.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), user["id"]))
    audit(user["id"], "login", username)
    return jsonify(success=True, redirect="/admin" if user["account_type"] == "Administrator" else "/dashboard", is_admin=user["account_type"] == "Administrator", csrf_token=session["csrf_token"])


@app.route("/api/logout", methods=["GET", "POST"])
def logout():
    uid = session.get("user_id"); session.clear()
    if uid: audit(uid, "logout")
    return jsonify(success=True)


@app.route("/api/current_user")
def me():
    user, err = require_user()
    if err: return err
    safe = {k: v for k, v in user.items() if k not in {"password_hash", "api_key_hash"}}
    safe["is_admin"] = user["account_type"] == "Administrator"; safe["subscription_active"] = subscription_active(user)
    return jsonify(success=True, user=safe, email=user["username"], plan="basic" if user["account_type"] == "User" else "admin", is_admin=user["account_type"] == "Administrator", impersonating=bool(session.get("admin_impersonator")), csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)))


@app.route("/api/admin/users")
def admin_users():
    user, err = require_user("users")
    if err: return err
    with db() as conn:
        rows = []
        for r in conn.execute("SELECT * FROM users ORDER BY id DESC"):
            item = dict(r); item["storage_used"] = user_storage(item["id"], refresh=True); item["storage_remaining"] = max(0, storage_limit_for(item) - item["storage_used"])
            item["server_count"] = conn.execute("SELECT COUNT(*) FROM servers WHERE owner_id=?", (item["id"],)).fetchone()[0]
            item["remaining_days"] = max(0, int((datetime.fromisoformat(item["subscription_expires_at"]) - datetime.now(timezone.utc)).total_seconds() // 86400)) if item.get("subscription_expires_at") else 0
            rows.append(item)
    return jsonify(success=True, users=rows)


@app.route("/api/admin/create-user", methods=["POST"])
def admin_create_user():
    actor, err = require_user("users")
    if err: return err
    d=json_body()
    try: username=validate_username(d.get("username") or d.get("email", ""))
    except ValueError as e: return jsonify(success=False,message=str(e)),400
    password=str(d.get("password", "")); days=int(d.get("subscription_days", d.get("expiry_days", 30)))
    if not password or not 1<=days<=365: return jsonify(success=False,message="Password and duration (1-365 days) are required"),400
    role=d.get("role", "Administrator" if d.get("is_admin") else "User"); account="Administrator" if role in {"Owner","Administrator","Manager","Support"} else "User"
    try:
        with db() as conn:
            cur=conn.execute("INSERT INTO users(username,password_hash,account_type,role,status,subscription_started_at,subscription_expires_at,max_servers,storage_per_server,storage_limit,storage_used,ram_per_server,cpu_per_server,max_backups,account_expiry,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (username,generate_password_hash(password,method="scrypt"),account,role,"ACTIVE",now_iso(),(datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),max(1,int(d.get("max_servers",3))),int(d.get("storage_per_server",d.get("storage_limit",1024**3))),int(d.get("storage_limit",d.get("storage_per_server",1024**3))),0,int(d.get("ram_per_server",512*1024**2)),float(d.get("cpu_per_server",1)),max(0,int(d.get("max_backups",3))),(datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),now_iso()))
            uid=cur.lastrowid
            (USERS_DIR/f"user_{uid}"/"servers").mkdir(parents=True,exist_ok=True)
    except sqlite3.IntegrityError: return jsonify(success=False,message="Username already exists"),409
    audit(actor["id"],"create_user",username); return jsonify(success=True,message="User created",user_id=uid,password_once=password)


@app.route("/api/admin/update-user", methods=["POST"])
def update_user():
    actor, err = require_user("users")
    if err: return err
    d=json_body(); uid=int(d.get("id",0) or 0)
    if not uid and d.get("email"):
        with db() as conn: found=conn.execute("SELECT id FROM users WHERE username=?",(str(d["email"]).lower(),)).fetchone()
        uid=found[0] if found else 0
    allowed={"status","role","max_servers","storage_per_server","storage_limit","ram_per_server","cpu_per_server","max_backups","subscription_expires_at","account_expiry"};
    if "expiry_days" in d: d["subscription_expires_at"]=(datetime.now(timezone.utc)+timedelta(days=int(d["expiry_days"]))).isoformat()
    if "storage_limit" in d: d["storage_per_server"]=int(d["storage_limit"])
    fields={k:d[k] for k in allowed if k in d}
    if not fields: return jsonify(success=False,message="No changes"),400
    if "role" in fields: fields["account_type"]="Administrator" if fields["role"] in {"Owner","Administrator","Manager","Support"} else "User"
    sql="UPDATE users SET "+",".join(f"{k}=?" for k in fields)+" WHERE id=?"
    with db() as conn: conn.execute(sql,(*fields.values(),uid))
    audit(actor["id"],"update_user",str(uid),fields); return jsonify(success=True)


@app.route("/api/admin/ban-user", methods=["POST"])
def ban_user():
    actor, err = require_user("users")
    if err: return err
    d = json_body(); uid = int(d.get("id", 0) or 0)
    if uid == actor["id"]: return jsonify(success=False, message="لا يمكن حظر حساب الأدمن الحالي"), 400
    with db() as conn:
        target = conn.execute("SELECT id,username FROM users WHERE id=?", (uid,)).fetchone()
        if not target: return jsonify(success=False, message="المستخدم غير موجود"), 404
        conn.execute("UPDATE users SET status='BANNED' WHERE id=?", (uid,))
    audit(actor["id"], "ban_user", str(uid), {"username": target["username"]})
    return jsonify(success=True, message="تم حظر المستخدم")


@app.route("/api/admin/unban-user", methods=["POST"])
def unban_user():
    actor, err = require_user("users")
    if err: return err
    d = json_body(); uid = int(d.get("id", 0) or 0)
    with db() as conn:
        target = conn.execute("SELECT id,username FROM users WHERE id=?", (uid,)).fetchone()
        if not target: return jsonify(success=False, message="المستخدم غير موجود"), 404
        conn.execute("UPDATE users SET status='ACTIVE' WHERE id=?", (uid,))
    audit(actor["id"], "unban_user", str(uid), {"username": target["username"]})
    return jsonify(success=True, message="تم فك حظر المستخدم")


@app.route("/api/admin/delete-user", methods=["POST"])
def delete_user():
    actor, err = require_user("users")
    if err: return err
    payload=json_body(); uid=int(payload.get("id",0) or 0)
    if not uid and payload.get("email"):
        with db() as conn: found=conn.execute("SELECT id FROM users WHERE username=?",(str(payload["email"]).lower(),)).fetchone()
        uid=found[0] if found else 0
    with db() as conn: conn.execute("DELETE FROM users WHERE id=? AND id!=?",(uid,actor["id"]))
    audit(actor["id"],"delete_user",str(uid)); return jsonify(success=True)


@app.route("/api/admin/add-plan", methods=["POST"])
def add_plan():
    actor, err = require_user("plans")
    if err: return err
    d = json_body(); plan_id = re.sub(r"[^a-z0-9_-]", "", str(d.get("plan_id", "")).lower())
    if not plan_id: return jsonify(success=False, message="Invalid plan id"), 400
    # Plans are intentionally represented as configuration in this compact build; account limits remain authoritative in users.
    audit(actor["id"], "add_plan", plan_id, d)
    return jsonify(success=True, message="Plan saved")

@app.route("/api/plans")
def plans(): return jsonify(success=True,plans={"basic":{"name":"Basic","storage":1024,"ram":512,"cpu":1,"max_servers":3},"pro":{"name":"Pro","storage":4096,"ram":2048,"cpu":2,"max_servers":10}})


@app.route("/api/servers")
def list_servers():
    user, err = require_user()
    if err: return err
    with db() as conn:
        q="SELECT s.*,u.username owner FROM servers s JOIN users u ON u.id=s.owner_id"; args=()
        if user["account_type"] != "Administrator": q += " WHERE s.owner_id=?"; args=(user["id"],)
        rows=[]
        for r in conn.execute(q,args):
            item=dict(r); owner_row = conn.execute("SELECT * FROM users WHERE id=?", (item["owner_id"],)).fetchone(); owner_data=dict(owner_row) if owner_row else user; item.update(folder=str(item["id"]), title=item["name"], plan="basic", server_type=item.get("server_type") or ("website" if item["language"] == "html" else "bot"), server_slug=item.get("server_slug") or item["slug"], server_language=item.get("server_language") or item["language"], website_url=item.get("website_url") or "", storage_limit=storage_limit_for(owner_data), storage_used=used_bytes(Path(item["path"]), refresh=True) if Path(item["path"]).exists() else 0, ram_limit=owner_data["ram_per_server"]//(1024**2), cpu_limit=owner_data["cpu_per_server"], is_admin=user["account_type"] == "Administrator")
            item["status"] = "Running" if item["status"] == "RUNNING" else item["status"].title()
            rows.append(item)
    used=sum(used_bytes(Path(r["path"]), refresh=True) for r in rows if Path(r["path"]).exists())
    return jsonify(success=True,servers=rows,stats={"used":len(rows),"total":user["max_servers"],"disk_used":used/(1024**2),"disk_total":storage_limit_for(user)/(1024**2),"expiry":user["subscription_expires_at"],"storage_used":used,"storage_limit":storage_limit_for(user),"storage_remaining":max(0,storage_limit_for(user)-used)})


@app.route("/api/server/add", methods=["POST"])
def add_server():
    user, err = require_user()
    if err: return err
    if not subscription_active(user): return jsonify(success=False,message="Subscription expired or inactive"),403
    d=json_body(); name=str(d.get("name","")).strip(); lang=str(d.get("language",d.get("type","python"))).lower(); lang={"javascript":"node","js":"node","c++":"cpp","html/css":"html"}.get(lang,lang)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}",name) or lang not in VALID_LANGUAGES: return jsonify(success=False,message="Use a unique slug with a-z, 0-9 and hyphens"),400
    server_type = "website" if str(d.get("type", "")).lower() == "website" or lang == "html" else "bot"
    with db() as conn:
        count=conn.execute("SELECT COUNT(*) FROM servers WHERE owner_id=?",(user["id"],)).fetchone()[0]
        if count>=user["max_servers"]: return jsonify(success=False,message="Maximum server limit reached"),403
        slug=name.lower();
        if conn.execute("SELECT 1 FROM servers WHERE slug=?", (slug,)).fetchone(): return jsonify(success=False,message="اسم الخادم مستخدم بالفعل، اختر اسمًا آخر"),409
        cur=conn.execute("INSERT INTO servers(owner_id,name,slug,language,path,status,port,server_type,server_slug,server_language,website_url,last_activity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(user["id"],name,slug,lang,"", "CREATED",allocate_port(),server_type,slug,lang,"",now_iso(),now_iso(),now_iso())); sid=cur.lastrowid
        path=USERS_DIR/f"user_{user['id']}"/"servers"/f"{sid}_{slug}"; path.mkdir(parents=True); conn.execute("UPDATE servers SET path=? WHERE id=?",(str(path),sid))
    website_base = get_setting("website_domain", "").strip().rstrip("/")
    website_url = (website_base + f"/site/{slug}") if server_type == "website" and website_base else (f"/site/{slug}" if server_type == "website" else "")
    with db() as conn: conn.execute("UPDATE servers SET website_url=? WHERE id=?", (website_url, sid))
    audit(user["id"],"create_server",str(sid),{"name":name,"language":lang,"type":server_type}); return jsonify(success=True,server={"id":sid,"name":name,"slug":slug,"server_type":server_type,"language":lang,"website_url":website_url,"status":"CREATED"})


@app.route("/api/server/action/<int:server_id>/<action>", methods=["POST"])
def server_action(server_id:int,action:str):
    user, err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False,message="Server not found"),404
    if action in {"start","restart"}:
        if action=="restart": stop_process(srv)
        ok,msg=start_process(user,srv)
    elif action in {"stop","kill"}: ok,msg=stop_process(srv,action=="kill")
    elif action=="delete":
        stop_process(srv,True)
        with db() as conn: conn.execute("DELETE FROM servers WHERE id=?",(server_id,))
        shutil.rmtree(srv["path"],ignore_errors=True); ok,msg=True,"Server deleted"
    else:return jsonify(success=False,message="Unknown action"),400
    audit(user["id"],action,str(server_id)); return jsonify(success=ok,message=msg)


@app.route("/api/server/stats/<int:server_id>")
def server_stats(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    p=psutil.Process(srv["pid"]) if srv.get("pid") and psutil.pid_exists(srv["pid"]) else None
    return jsonify(success=True,status=("Running" if srv["status"] == "RUNNING" else srv["status"].title()),pid=srv.get("pid"),uptime=(time.time()-srv["start_time"] if srv.get("start_time") else 0),cpu=(p.cpu_percent(0.1) if p else 0),ram=(p.memory_info().rss if p else 0),mem=(f"{(p.memory_info().rss/(1024**2)):.1f} MB" if p else "0 MB"),storage=used_bytes(Path(srv["path"])),logs=(Path(srv["path"])/"console.log").read_text(encoding="utf-8",errors="replace")[-100000:] if (Path(srv["path"])/"console.log").exists() else "")


def file_response(srv:dict[str,Any], relative:str):
    return safe_child(Path(srv["path"]),relative)


@app.route("/api/files/list/<int:server_id>")
def files_list(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    rel=request.args.get("path",""); base=file_response(srv,rel)
    if not base.exists() or not base.is_dir():return jsonify(success=False,message="Directory not found"),404
    files=[]
    for p in sorted(base.iterdir(),key=lambda x:(not x.is_dir(),x.name.lower())):
        files.append({"name":p.name,"path":str(p.relative_to(Path(srv["path"]))),"is_dir":p.is_dir(),"size":p.stat().st_size if p.is_file() else 0,"modified":p.stat().st_mtime})
    return jsonify(success=True, files=files)


@app.route("/api/files/content/<int:server_id>/<path:filename>")
def file_content(server_id,filename):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    p=file_response(srv,filename)
    if not p.is_file() or p.stat().st_size>5*1024*1024:return jsonify(success=False,message="File unavailable"),400
    return jsonify(success=True,content=p.read_text(encoding="utf-8",errors="replace"))


@app.route("/api/files/save/<int:server_id>/<path:filename>",methods=["POST"])
def file_save(server_id,filename):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); p=file_response(srv,filename) if srv else None
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    if not p:return jsonify(success=False),404
    content=str(json_body().get("content","")); old=user_storage(user["id"], refresh=True); existing=p.stat().st_size if p.exists() else 0
    if old-existing+len(content.encode())>storage_limit_for(user):return jsonify(success=False,message="Storage quota exceeded"),413
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(content,encoding="utf-8"); audit(user["id"],"edit_file",filename); return jsonify(success=True)


@app.route("/api/files/upload/<int:server_id>",methods=["POST"])
def file_upload(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    rel=request.form.get("path","")
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    dest=file_response(srv,rel); dest.mkdir(parents=True,exist_ok=True)
    saved=[]
    uploaded = request.files.getlist("file") or request.files.getlist("files") or request.files.getlist("files[]")
    if not uploaded or all(not (f.filename or "").strip() for f in uploaded):
        return jsonify(success=False,message="No files were selected"),400
    for f in uploaded:
        name=secure_filename(f.filename or "")
        if not name:continue
        target=safe_child(dest,name); data=f.read()
        if user_storage(user["id"], refresh=True)+len(data)>storage_limit_for(user):return jsonify(success=False,message="Storage quota exceeded"),413
        target.write_bytes(data); saved.append(name)
    if not saved:
        return jsonify(success=False,message="No valid files were uploaded"),400
    audit(user["id"],"upload",str(server_id),saved); return jsonify(success=True,files=saved,message="Files uploaded successfully")


@app.route("/api/files/create/<int:server_id>",methods=["POST"])
def file_create(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); d=json_body(); name=secure_filename(str(d.get("name") or d.get("filename") or ""))
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    is_dir=bool(d.get("is_dir",d.get("type")=="folder"))
    if not srv or not name:return jsonify(success=False,message="Invalid file name"),400
    rel=str(d.get("path", "")); p=file_response(srv, str(Path(rel) / name) if rel else name)
    if is_dir:
        p.mkdir(parents=True,exist_ok=False)
    else:
        content=str(d.get("content", ""));
        if user_storage(user["id"], refresh=True) + len(content.encode()) > storage_limit_for(user): return jsonify(success=False,message="Storage quota exceeded"),413
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text(content,encoding="utf-8")
    audit(user["id"],"create_file",str(server_id),str(p.relative_to(Path(srv["path"]))))
    return jsonify(success=True)


@app.route("/api/files/delete/<int:server_id>",methods=["POST"])
def file_delete(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); names=json_body().get("files",[json_body().get("name","")])
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    if not srv:return jsonify(success=False),404
    for name in names:
        p=file_response(srv,str(name))
        if p == Path(srv["path"]):continue
        if p.is_dir():shutil.rmtree(p)
        elif p.is_file():p.unlink()
    audit(user["id"],"delete_file",str(server_id),names); return jsonify(success=True)


@app.route("/api/files/rename/<int:server_id>",methods=["POST"])
def file_rename(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); d=json_body(); old=str(d.get("old") or d.get("from") or d.get("old_name") or "")
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    new=secure_filename(str(d.get("new") or d.get("to") or d.get("new_name") or ""))
    if not srv or not old or not new:return jsonify(success=False,message="Invalid rename request"),400
    src=file_response(srv,old); dst=file_response(srv,str(Path(old).parent/new))
    if not src.exists() or dst.exists(): return jsonify(success=False,message="Source missing or destination exists"),409
    src.rename(dst); audit(user["id"],"rename_file",str(server_id),{"old":old,"new":new}); return jsonify(success=True)


@app.route("/api/files/unzip/<int:server_id>/<path:filename>",methods=["POST"])
def unzip_api(server_id,filename):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); src=file_response(srv,filename) if srv else None
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    if not src or not src.is_file() or not zipfile.is_zipfile(src):return jsonify(success=False,message="Invalid ZIP"),400
    root=Path(srv["path"])
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            target=safe_child(root,info.filename)
            if info.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as inp:
                data=inp.read(max(0, storage_limit_for(user)-user_storage(user["id"], refresh=True))+1)
            if user_storage(user["id"], refresh=True)+len(data)>storage_limit_for(user):return jsonify(success=False,message="Storage quota exceeded"),413
            target.write_bytes(data)
    audit(user["id"],"extract_zip",filename); return jsonify(success=True)


@app.route("/api/server/set-startup/<int:server_id>", methods=["POST"])
def set_startup(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    filename = str(json_body().get("filename", ""))
    p = file_response(srv, filename)
    if not filename or not p.is_file(): return jsonify(success=False, message="Startup file not found"), 400
    with db() as conn: conn.execute("UPDATE servers SET startup_file=?,updated_at=? WHERE id=?", (str(p.relative_to(Path(srv["path"]))), now_iso(), server_id))
    audit(user["id"], "set_startup", str(server_id), filename)
    return jsonify(success=True, startup_file=filename)


@app.route("/api/create_api_key", methods=["POST"])
def create_api_key():
    user, err = require_user()
    if err: return err
    raw = "ygh_" + secrets.token_urlsafe(32)
    with db() as conn: conn.execute("UPDATE users SET api_key_hash=? WHERE id=?", (generate_password_hash(raw, method="scrypt"), user["id"]))
    audit(user["id"], "create_api_key")
    return jsonify(success=True, api_key=raw)


@app.route("/api/admin/maintenance", methods=["GET", "POST"])
def maintenance():
    user, err = require_user("security")
    if err: return err
    if request.method == "POST":
        d = json_body(); enabled = bool(d.get("enabled", d.get("maintenance", False)))
        set_setting("maintenance", "1" if enabled else "0")
        set_setting("maintenance_message", str(d.get("message", "الموقع في وضع الصيانة"))[:500])
        audit(user["id"], "maintenance", details={"enabled": enabled})
    return jsonify(success=True, enabled=get_setting("maintenance", "0") == "1", message=get_setting("maintenance_message", "الموقع في وضع الصيانة"))


@app.route("/api/admin/broadcast", methods=["POST"])
def broadcast():
    user, err = require_user("users")
    if err: return err
    d = json_body(); message = str(d.get("message", "")).strip(); audience = str(d.get("audience", "all"))
    if not message or len(message) > 5000 or audience not in {"all", "active", "unbanned"}: return jsonify(success=False, message="Invalid broadcast"), 400
    with db() as conn:
        where = "1=1" if audience == "all" else ("status='ACTIVE'" if audience in {"active", "unbanned"} else "1=1")
        targeted = conn.execute(f"SELECT COUNT(*) FROM users WHERE {where}").fetchone()[0]
        cur = conn.execute("INSERT INTO broadcasts(actor_id,message,audience,targeted,successful,created_at) VALUES(?,?,?,?,?,?)", (user["id"], message, audience, targeted, targeted, now_iso()))
    audit(user["id"], "broadcast", str(cur.lastrowid), {"audience": audience, "targeted": targeted})
    return jsonify(success=True, targeted=targeted, successful=targeted, failed=0)


@app.route("/api/server/install/<int:server_id>",methods=["POST"])
def install(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    path=Path(srv["path"]); lang=srv["language"]
    if lang=="python" and (path/"requirements.txt").is_file(): cmd=["python3","-m","venv",".venv"]; cmd2=[str(path/".venv"/"bin"/"pip"),"install","-r","requirements.txt"]
    elif lang in {"node","javascript"} and (path/"package.json").is_file(): cmd=["npm","install"]; cmd2=None
    elif lang=="cpp":
        sources=[str(x.name) for x in path.glob("*.cpp")]
        if not sources: return jsonify(success=False,message="No C++ source files found"),400
        cmd=["g++","-O2",*sources,"-o",".host_build"]; cmd2=None
    elif lang=="c":
        sources=[str(x.name) for x in path.glob("*.c")]
        if not sources: return jsonify(success=False,message="No C source files found"),400
        cmd=["gcc","-O2",*sources,"-o",".host_build"]; cmd2=None
    else:return jsonify(success=False,message="No dependency manifest found"),400
    append_log(server_id,"system",f"Installing: {' '.join(cmd)}")
    try:
        subprocess.run(cmd,cwd=path,check=True,capture_output=True,text=True,shell=False)
        if cmd2: subprocess.run(cmd2,cwd=path,check=True,capture_output=True,text=True,shell=False)
    except subprocess.CalledProcessError as e: append_log(server_id,"stderr",e.stdout+e.stderr); return jsonify(success=False,message="Installation failed"),400
    return jsonify(success=True)


@app.route("/api/server/console/<int:server_id>")
def console(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    lines=[]; log=Path(srv["path"])/"console.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8",errors="replace").splitlines()[-1000:]:
            try:lines.append(json.loads(line))
            except:lines.append({"ts":now_iso(),"stream":"system","line":line})
    return jsonify(success=True,lines=lines)


@app.route("/api/server/console/<int:server_id>/input", methods=["POST"])
def console_input(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    command = str(json_body().get("command", ""))
    if not command or len(command) > 2000:
        return jsonify(success=False, message="Enter a command up to 2000 characters"), 400
    with process_lock:
        proc = processes.get(server_id)
    if not proc or proc.poll() is not None or proc.stdin is None:
        return jsonify(success=False, message="Server is not running or does not accept input"), 409
    try:
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        return jsonify(success=False, message="The server process closed its input") , 409
    append_log(server_id, "input", command)
    audit(user["id"], "console_input", str(server_id))
    return jsonify(success=True, message="Command sent")


@app.route("/api/server/console/<int:server_id>/stream")
def console_stream(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    def generate() -> Iterator[str]:
        log=Path(srv["path"])/"console.log"; pos=0
        while True:
            if log.exists():
                with log.open(encoding="utf-8",errors="replace") as f:
                    f.seek(pos)
                    chunk=f.read(); pos=f.tell()
                for line in chunk.splitlines(): yield f"data: {line}\n\n"
            with log_conditions.setdefault(server_id,threading.Condition()): log_conditions[server_id].wait(timeout=2)
            yield ": heartbeat\n\n"
    return Response(generate(),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/api/backups/<int:server_id>",methods=["GET","POST","DELETE"])
def backups(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    root=Path(srv["path"])
    if request.method=="POST":
        with db() as conn:
            if conn.execute("SELECT COUNT(*) FROM backups WHERE server_id=?",(server_id,)).fetchone()[0]>=user["max_backups"]:return jsonify(success=False,message="Backup limit reached"),403
            filename=f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"; target=DATA_DIR/"backups"/str(server_id); target.mkdir(parents=True,exist_ok=True); archive=target/filename
            with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
                for p in root.rglob("*"):
                    if p.is_file() and p.name not in {"console.log"}:z.write(p,p.relative_to(root))
            conn.execute("INSERT INTO backups(server_id,filename,size,created_at) VALUES(?,?,?,?)",(server_id,filename,archive.stat().st_size,now_iso()))
        return jsonify(success=True,filename=filename)
    with db() as conn: rows=[dict(r) for r in conn.execute("SELECT * FROM backups WHERE server_id=? ORDER BY id DESC",(server_id,))]
    if request.method == "DELETE":
        backup_id = int(request.args.get("id", 0) or 0)
        with db() as conn: row = conn.execute("SELECT * FROM backups WHERE id=? AND server_id=?", (backup_id, server_id)).fetchone()
        if not row: return jsonify(success=False, message="Backup not found"), 404
        (DATA_DIR / "backups" / str(server_id) / row["filename"]).unlink(missing_ok=True)
        with db() as conn: conn.execute("DELETE FROM backups WHERE id=?", (backup_id,))
        audit(user["id"], "delete_backup", str(backup_id)); return jsonify(success=True)
    return jsonify(success=True,backups=rows)


@app.route("/api/backups/<int:server_id>/download/<int:backup_id>")
def backup_download(server_id, backup_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    with db() as conn:
        row = conn.execute("SELECT * FROM backups WHERE id=? AND server_id=?", (backup_id, server_id)).fetchone()
    if not row: return jsonify(success=False, message="Backup not found"), 404
    archive = DATA_DIR / "backups" / str(server_id) / row["filename"]
    if not archive.is_file(): return jsonify(success=False, message="Backup file missing"), 404
    return send_file(archive, as_attachment=True, download_name=row["filename"], mimetype="application/zip")


@app.route("/site/<slug>", defaults={"filename": "index.html"})
@app.route("/site/<slug>/<path:filename>")
def website(slug, filename):
    with db() as conn: srv = conn.execute("SELECT * FROM servers WHERE slug=? AND server_type='website'", (slug,)).fetchone()
    if not srv: return "Website not found", 404
    root = Path(srv["path"]).resolve()
    try: target = safe_child(root, filename)
    except ValueError: return "Not found", 404
    if not target.is_file(): return "File not found", 404
    return send_file(target)


@app.route("/api/admin/settings", methods=["GET", "POST"])
def admin_settings():
    actor, err = require_user("security")
    if err: return err
    if request.method == "POST":
        d = json_body(); domain = str(d.get("website_domain", "")).strip().rstrip("/")
        if domain and not re.fullmatch(r"https?://[^\s/]+(?::\d+)?", domain): return jsonify(success=False, message="Invalid website domain"), 400
        set_setting("website_domain", domain); audit(actor["id"], "update_settings", "website_domain")
    return jsonify(success=True, website_domain=get_setting("website_domain", ""))


@app.route("/api/admin/impersonate/<int:user_id>", methods=["POST"])
def impersonate(user_id):
    actor, err = require_user("users")
    if err: return err
    with db() as conn: target = rowdict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    if not target: return jsonify(success=False, message="User not found"), 404
    session["admin_impersonator"] = actor["id"]; session["user_id"] = target["id"]; session["csrf_token"] = secrets.token_urlsafe(32)
    audit(actor["id"], "admin_impersonation_start", str(user_id))
    return jsonify(success=True, redirect="/dashboard")


@app.route("/api/admin/impersonate/return", methods=["POST"])
def return_from_impersonation():
    admin_id = session.get("admin_impersonator")
    if not admin_id: return jsonify(success=False, message="No temporary admin session"), 400
    old = session.get("user_id"); session["user_id"] = admin_id; session.pop("admin_impersonator", None); session["csrf_token"] = secrets.token_urlsafe(32)
    audit(admin_id, "admin_impersonation_end", str(old or ""))
    return jsonify(success=True, redirect="/admin")


@app.route("/api/system/metrics")
def metrics():
    user,err=require_user();
    if err:return err
    with db() as conn:
        stats={"total_users":conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],"total_servers":conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0],"active_servers":conn.execute("SELECT COUNT(*) FROM servers WHERE status='RUNNING'").fetchone()[0]}
    stats.update(cpu=psutil.cpu_percent(),ram=psutil.virtual_memory().percent,memory=psutil.virtual_memory().percent,disk=psutil.disk_usage(str(DATA_DIR)).percent); return jsonify(success=True,**stats)


@app.route("/api/ping")
def ping(): return jsonify(success=True,brand=BRAND)


@app.route("/api/admin/activity")
def activity():
    user,err=require_user("security");
    if err:return err
    with db() as conn: rows=[dict(r) for r in conn.execute("SELECT l.*,u.username FROM activity_logs l LEFT JOIN users u ON u.id=l.actor_id ORDER BY l.id DESC LIMIT 500")]
    return jsonify(success=True,logs=rows)


@app.errorhandler(Exception)
def unhandled(exc):
    app.logger.exception("Unhandled request error")
    return jsonify(success=False,message="Internal server error"),500


if __name__ == "__main__":
    port=int(os.environ.get("PORT",5000)); app.run(host="0.0.0.0",port=port,debug=False,threaded=True)
