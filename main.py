from __future__ import annotations

import io
import json
import logging
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
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", 512 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    SESSION_COOKIE_SAMESITE="Lax",
)

BRAND = "•-تــيــ۾ إڪـXـس-•"
VALID_LANGUAGES = {"python", "php", "nodejs", "node", "javascript", "java", "ruby", "go", "rust", "elixir", "deno", "shell", "c", "cpp", "html", "image"}
LANGUAGE_ALIASES = {"javascript": "nodejs", "js": "nodejs", "node": "nodejs", "c++": "cpp", "html/css": "html", "static": "html", "image hosting": "image", "images": "image"}
LANGUAGE_LABELS = {"python": "PYTHON", "php": "PHP", "nodejs": "Node.js", "java": "Java", "ruby": "Ruby", "go": "Go", "rust": "Rust", "elixir": "Elixir", "deno": "Deno", "shell": "Shell", "c": "C", "cpp": "C++", "html": "HTML", "image": "Image Hosting"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

def valid_image_bytes(data: bytes, suffix: str) -> bool:
    signatures = {".png": data.startswith(b"\x89PNG\r\n\x1a\n"), ".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"), ".gif": data.startswith((b"GIF87a", b"GIF89a")), ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    return bool(signatures.get(suffix.lower(), False))
UNLIMITED_MAX_SERVERS = 1_000_000_000
UNLIMITED_STORAGE = 1_000_000_000_000_000
UNLIMITED_RAM = 1_000_000_000_000_000
UNLIMITED_CPU = 1_000_000.0
UNLIMITED_BACKUPS = 1_000_000_000
SERVER_STATES = {"CREATED", "STARTING", "RUNNING", "STOPPING", "STOPPED", "CRASHED", "ERROR", "INSTALLING", "BUILDING"}
processes: dict[int, subprocess.Popen] = {}
process_lock = threading.RLock()
log_conditions: dict[int, threading.Condition] = {}
start_reservations: set[int] = set()
crash_history: dict[int, list[float]] = {}
install_jobs: dict[str, dict[str, Any]] = {}
install_jobs_lock = threading.RLock()
MAX_LOG_BYTES = int(os.environ.get("MAX_LOG_BYTES", 10 * 1024 * 1024))
runtime_cache: dict[str, tuple[float, dict[str, Any]]] = {}
runtime_cache_lock = threading.Lock()
RUNTIME_CACHE_SECONDS = int(os.environ.get("RUNTIME_CACHE_SECONDS", "60"))


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
            CREATE TABLE IF NOT EXISTS images (
              id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
              storage_name TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
              mime_type TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_servers_owner ON servers(owner_id);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON activity_logs(created_at);
            """
        )
        # Additive migrations: preserve all existing tables and data while introducing
        # account quotas, explicit server types, and website metadata.
        migrations = {
            "users": {"storage_limit": "INTEGER NOT NULL DEFAULT 1073741824", "storage_used": "INTEGER NOT NULL DEFAULT 0", "account_expiry": "TEXT"},
            "servers": {"server_type": "TEXT NOT NULL DEFAULT 'bot'", "server_slug": "TEXT", "server_language": "TEXT", "website_url": "TEXT", "last_activity": "TEXT", "runtime_mode": "TEXT", "webhook_path": "TEXT", "php_suggested_mode": "TEXT"},
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
        # Preserve audit history while repairing legacy rows whose actor was deleted.
        conn.execute("UPDATE activity_logs SET actor_id=NULL WHERE actor_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users WHERE users.id=activity_logs.actor_id)")
        # Archives may contain absolute paths from another checkout. Re-anchor them
        # without losing the extracted user files: the archive commonly contains
        # BASE_DIR/data/users while the database points at the old checkout's
        # /.../data/users location.
        for row in conn.execute("SELECT id,owner_id,path,slug FROM servers").fetchall():
            old = Path(row[2]) if row[2] else Path()
            target = USERS_DIR / f"user_{row[1]}" / "servers" / f"{row[0]}_{row[3]}"
            if old != target and (not old.exists() or str(old).startswith(str(BASE_DIR)) is False):
                # Prefer the archive's already-extracted directory, preserving
                # files, hidden environments, bot logs, and database contents.
                parts = old.parts
                if "users" in parts:
                    suffix = parts[parts.index("users") + 1:]
                    candidate = USERS_DIR.joinpath(*suffix)
                    if candidate.exists():
                        target = candidate
                target.mkdir(parents=True, exist_ok=True)
                conn.execute("UPDATE servers SET path=? WHERE id=?", (str(target), row[0]))

        # A persisted RUNNING state is not proof that a process survived an app
        # restart. Clear only records whose PID is definitely gone; never kill a
        # potentially reused PID during startup.
        for row in conn.execute("SELECT id,pid,status FROM servers WHERE pid IS NOT NULL").fetchall():
            if not psutil.pid_exists(int(row[1])):
                conn.execute("UPDATE servers SET pid=NULL,status=?,exit_code=?,updated_at=? WHERE id=?",
                             ("CRASHED" if row[2] == "RUNNING" else row[2], None, now_iso(), row[0]))

        # Requested built-in credentials. Prefer ADMIN_USERNAME/ADMIN_PASSWORD in production.
        admin_user = os.environ.get("ADMIN_USERNAME", "Admin@gmail.com").strip().lower()
        admin_password = os.environ.get("ADMIN_PASSWORD")
        exists = conn.execute("SELECT id FROM users WHERE username=?", (admin_user,)).fetchone()
        if not exists:
            if not admin_password:
                admin_password = secrets.token_urlsafe(18) + "A1"
                print(f"Generated initial admin password (set ADMIN_PASSWORD to replace it): {admin_password}")
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
    if relative.startswith("/") or ".." in Path(relative).parts:
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


def normalize_language(value: str) -> str:
    value = str(value or "python").strip().lower()
    return LANGUAGE_ALIASES.get(value, value)


def normalize_php_mode(value: str | None) -> str:
    value = str(value or "").strip().lower().replace("-", "_")
    aliases = {"bot": "cli", "cli_bot": "cli", "long_polling": "cli", "web": "website", "api": "website", "webhook_bot": "webhook"}
    return aliases.get(value, value) if value in {"cli", "bot", "cli_bot", "long_polling", "webhook", "webhook_bot", "website", "web", "api"} else ""


def detect_php_project(path: Path) -> dict[str, Any]:
    """Return a suggestion and evidence; the caller must still choose the mode."""
    files = {p.name.lower() for p in path.iterdir()} if path.is_dir() else set()
    sources = []
    for source in path.rglob("*.php") if path.is_dir() else []:
        if any(part in {"vendor", ".git"} for part in source.parts):
            continue
        sources.append(source.read_text(encoding="utf-8", errors="ignore")[:250_000])
    source = "\n".join(sources)
    signals: list[str] = []
    cli_score = webhook_score = website_score = 0
    if "composer.json" in files: signals.append("composer.json")
    if any(name in files for name in {"index.php", "main.php", "app.php", "server.php", "bot.php"}): website_score += 1; signals.append("PHP entry point")
    if re.search(r"getupdates|long.?poll|while\s*\(.*true|sleep\s*\(", source, re.I): cli_score += 3; signals.append("long-polling/getUpdates pattern")
    if re.search(r"setwebhook|webhook|php:\/\/input", source, re.I): webhook_score += 3; signals.append("webhook pattern")
    if re.search(r"\$_(GET|POST|REQUEST|SERVER)|session_start|<html|<!doctype|header\s*\(", source, re.I): website_score += 2; signals.append("HTTP/web page pattern")
    if "public" in files or "views" in files or "routes" in files: website_score += 1; signals.append("web application directories")
    scores = {"cli": cli_score, "webhook": webhook_score, "website": website_score}
    suggested = max(scores, key=scores.get) if max(scores.values()) else "cli"
    return {"suggested_mode": suggested, "scores": scores, "signals": signals, "modes": ["cli", "webhook", "website"]}

def runtime_probe(language: str, force: bool = False) -> dict[str, Any]:
    """Probe an executable and a minimal script, then cache the result briefly."""
    language = normalize_language(language)
    now = time.time()
    with runtime_cache_lock:
        cached = runtime_cache.get(language)
        if cached and not force and now - cached[0] < RUNTIME_CACHE_SECONDS:
            return dict(cached[1])
    commands = {
        "python": (os.environ.get("PYTHON_BIN", "python3"), ["-c", "print('PYTHON_RUNTIME_OK')"]),
        "nodejs": ("node", ["-e", "console.log('NODE_RUNTIME_OK')"]),
        "php": ("php", ["-r", "echo 'PHP_RUNTIME_OK';"]),
    }
    executable = commands.get(language, (None, None))[0]
    result: dict[str, Any] = {"language": language, "available": language in {"html", "image"}, "executable": None, "version": None, "test_output": None, "reason": ""}
    if executable:
        path = shutil.which(executable)
        result["executable"] = path
        if not path:
            result["reason"] = f"{executable} executable not found"
        else:
            try:
                version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
                smoke = subprocess.run([path, *commands[language][1]], capture_output=True, text=True, timeout=10)
                result["version"] = (version.stdout or version.stderr).splitlines()[0][:120] if version.returncode == 0 else None
                result["test_output"] = (smoke.stdout or smoke.stderr).strip()[:200]
                result["available"] = version.returncode == 0 and smoke.returncode == 0
                if not result["available"]:
                    result["reason"] = f"runtime test failed with exit code {smoke.returncode}"
                if language == "php":
                    modules = subprocess.run([path, "-m"], capture_output=True, text=True, timeout=10)
                    loaded = {line.strip().lower() for line in modules.stdout.splitlines() if line.strip() and not line.startswith("[")}
                    required = {"curl", "json", "mbstring", "openssl", "pdo"}
                    result["php_cli_available"] = version.returncode == 0
                    result["php_cgi_executable"] = shutil.which("php-cgi")
                    result["php_extensions"] = {name: name in loaded for name in sorted(required | {"sqlite3", "zip"})}
                    missing = sorted(name for name in required if name not in loaded)
                    if missing:
                        result["reason"] = "Missing PHP extensions: " + ", ".join(missing)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["reason"] = str(exc)
    if language == "nodejs":
        npm = shutil.which("npm")
        result["npm_executable"] = npm
        result["npm_available"] = bool(npm)
    with runtime_cache_lock:
        runtime_cache[language] = (now, dict(result))
    return result

def first_file_case_insensitive(path: Path, names: tuple[str, ...]) -> Path | None:
    """Return the first matching file without assuming a case-sensitive FS."""
    for name in names:
        relative = name.replace("\\", "/")
        parent = path.joinpath(*Path(relative).parts[:-1]) if "/" in relative else path
        leaf = Path(relative).name
        by_lower = {entry.name.lower(): entry for entry in parent.iterdir()} if parent.is_dir() else {}
        candidate = by_lower.get(leaf.lower())
        if candidate and candidate.is_file():
            return candidate
    return None

def runtime_command(srv: dict[str, Any]) -> list[str] | None:
    path = Path(srv["path"]).resolve()
    lang = normalize_language(srv.get("language"))
    if lang == "image": return None
    startup = srv.get("startup_file")
    if startup:
        startup_path = safe_child(path, startup)
        if not startup_path.is_file():
            raise RuntimeError(f"Startup file not found: {startup}")
        startup = str(startup_path.relative_to(path))
    if lang == "python":
        entry = first_file_case_insensitive(path, (startup,) if startup else ("main.py", "app.py", "bot.py", "server.py", "index.py", "run.py"))
        startup = str(entry.relative_to(path)) if entry else None
        interpreter = path / ".venv" / "bin" / "python"
        interpreter = str(interpreter) if interpreter.is_file() else (shutil.which(os.environ.get("PYTHON_BIN", "python3")) or shutil.which("python") or "")
        if not interpreter or not runtime_probe("python")["available"]: raise RuntimeError("Python runtime is unavailable")
        return [interpreter, "-u", startup] if startup else None
    if lang == "nodejs":
        node = shutil.which("node")
        if not node: raise RuntimeError("Node.js runtime is not installed.")
        if startup: return [node, startup]
        if (path / "package.json").is_file():
            npm = shutil.which("npm")
            if not npm: raise RuntimeError("Node.js is available, but npm is missing; install npm before using package.json scripts.")
            try:
                package = json.loads((path / "package.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid package.json: {exc}")
            if isinstance(package.get("scripts"), dict) and package["scripts"].get("start"):
                return [npm, "start"]
        startup = next((x for x in ("index.js", "server.js", "app.js", "main.js", "bot.js") if (path / x).is_file()), None)
        return [node, startup] if startup else None
    if lang == "php":
        php = shutil.which("php")
        if not php: raise RuntimeError("PHP runtime is not installed.")
        if not runtime_probe("php")["available"]: raise RuntimeError("PHP runtime is unavailable")
        bootstrap = Path(__file__).resolve().parent / "runtime" / "php_bootstrap.php"
        php_prefix = [php, "-d", f"auto_prepend_file={bootstrap}"] if bootstrap.is_file() else [php]
        entry = first_file_case_insensitive(path, (startup,) if startup else ("index.php", "server.php", "app.php", "main.php"))
        startup = str(entry.relative_to(path)) if entry else None
        if not startup: return None
        mode = normalize_php_mode(srv.get("runtime_mode") or srv.get("php_mode"))
        if not mode:
            mode = "website" if str(srv.get("server_type", "bot")).lower() == "website" else "cli"
        # Webhook projects are served by the PHP built-in server only when the
        # user explicitly selected webhook mode. Telegram/source keywords are
        # suggestions, never a reason to reject a CLI bot.
        if mode in {"website", "webhook"}:
            return php_prefix + ["-S", f"0.0.0.0:{srv.get('port') or 0}", "-t", str(path)]
        return php_prefix + [startup]
    if lang in {"c", "cpp"}:
        compiler = shutil.which("gcc" if lang == "c" else "g++")
        if not compiler: raise RuntimeError(f"{'C' if lang == 'c' else 'C++'} compiler ({'gcc' if lang == 'c' else 'g++'}) is not installed.")
        return [str(path / ".host_build")] if (path / ".host_build").is_file() else None
    if lang == "java":
        java = shutil.which("java")
        if not java: raise RuntimeError("Java runtime is not installed.")
        jars = sorted((path / "target").glob("*.jar")) if (path / "target").is_dir() else []
        return [java, "-jar", str(jars[0])] if jars else None
    if lang == "ruby":
        ruby = shutil.which("ruby")
        if not ruby: raise RuntimeError("Ruby runtime is not installed.")
        entry = first_file_case_insensitive(path, (startup,) if startup else ("main.rb", "app.rb", "server.rb"))
        startup = str(entry.relative_to(path)) if entry else None
        return [ruby, startup] if startup else None
    if lang == "go":
        go = shutil.which("go")
        if not go: raise RuntimeError("Go runtime is not installed.")
        return [str(path / ".host_build")] if (path / ".host_build").is_file() else ([go, "run", "."] if (path / "go.mod").is_file() else None)
    if lang == "rust":
        if not shutil.which("cargo"): raise RuntimeError("Rust/Cargo runtime is not installed.")
        return ["cargo", "run", "--release"] if (path / "Cargo.toml").is_file() else None
    if lang == "elixir":
        if not shutil.which("mix"): raise RuntimeError("Elixir/Mix runtime is not installed.")
        return ["mix", "run", "--no-halt"] if (path / "mix.exs").is_file() else None
    if lang == "deno":
        deno = shutil.which("deno")
        if not deno: raise RuntimeError("Deno runtime is not installed.")
        entry = first_file_case_insensitive(path, (startup,) if startup else ("main.ts", "main.js", "mod.ts"))
        startup = str(entry.relative_to(path)) if entry else None
        return [deno, "run", "--allow-all", startup] if startup else None
    if lang == "shell":
        sh = shutil.which("bash") or shutil.which("sh")
        entry = first_file_case_insensitive(path, (startup,) if startup else ("start.sh", "main.sh", "run.sh"))
        startup = str(entry.relative_to(path)) if entry else None
        return [sh, startup] if sh and startup else None
    if lang == "html":
        return [shutil.which("python3") or "python3", "-m", "http.server", str(srv.get("port") or 0)]
    return None


def preexec_for(user: dict[str, Any], language: str = ""):
    def setup():
        os.setsid()
        try:
            import resource
            # V8 and the JVM reserve virtual address space beyond their heap
            # quota at startup. A 512 MiB RLIMIT_AS kills Node with SIGTRAP
            # before user code runs, so keep the hard limit while providing a
            # 1 GiB minimum reservation for runtimes that need it.
            ram_limit = int(user["ram_per_server"])
            if normalize_language(language) in {"nodejs", "php", "java"}:
                ram_limit = max(ram_limit, 1024 * 1024 * 1024)
            resource.setrlimit(resource.RLIMIT_AS, (ram_limit, ram_limit))
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
        if log.stat().st_size > MAX_LOG_BYTES:
            rotated = log.with_name("console.log.1")
            try:
                rotated.unlink(missing_ok=True)
                log.replace(rotated)
                log.touch()
            except OSError:
                pass
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
        srv = conn.execute("SELECT owner_id,auto_restart,status FROM servers WHERE id=?", (server_id,)).fetchone()
        if srv:
            stopping = srv["status"] in {"STOPPING", "STOPPED"}
            state = "ERROR" if srv["status"] == "ERROR" else ("CRASHED" if code and not stopping else "STOPPED")
            conn.execute("UPDATE servers SET pid=NULL,status=?,exit_code=?,updated_at=? WHERE id=?", (state, code, now_iso(), server_id))
    append_log(server_id, "system", f"Process exited with code {code}")
    if srv and code and not stopping and srv["auto_restart"]:
        now = time.time()
        history = [t for t in crash_history.get(server_id, []) if now - t < 600]
        history.append(now)
        crash_history[server_id] = history
        if len(history) >= 5:
            with db() as conn:
                conn.execute("UPDATE servers SET status='ERROR',updated_at=? WHERE id=?", (now_iso(), server_id))
            append_log(server_id, "system", "Crash loop protection: automatic restart paused after 5 crashes in 10 minutes")
        else:
            delay = min(60, 2 ** (len(history) - 1))
            append_log(server_id, "system", f"Auto-restart scheduled in {delay}s (attempt {len(history)}/5)")
            def restart_later(owner_id: int, wait: int) -> None:
                time.sleep(wait)
                with db() as conn:
                    user_row = conn.execute("SELECT * FROM users WHERE id=?", (owner_id,)).fetchone()
                    server_row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
                if user_row and server_row and server_row["status"] == "CRASHED":
                    ok, message = start_process(dict(user_row), dict(server_row))
                    append_log(server_id, "system", f"Auto-restart {'succeeded' if ok else 'failed'}: {message}")
            threading.Thread(target=restart_later, args=(srv["owner_id"], delay), daemon=True).start()
    notify(server_id)


PYPI_IMPORT_MAP = {"flask": "Flask", "requests": "requests", "telebot": "pyTelegramBotAPI", "dotenv": "python-dotenv", "telegram": "python-telegram-bot", "discord": "discord.py", "aiohttp": "aiohttp", "pytz": "pytz"}
PYTHON_STDLIB = {"os", "sys", "json", "re", "time", "math", "random", "datetime", "pathlib", "asyncio", "sqlite3", "subprocess", "threading", "typing", "collections", "itertools", "logging", "socket", "http", "email", "io", "base64", "hashlib", "secrets"}

def infer_python_requirements(path: Path) -> list[str]:
    found = set()
    for source in path.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in source.parts): continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        found.update(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", text, flags=re.M))
    return sorted(PYPI_IMPORT_MAP[name] for name in found if name not in PYTHON_STDLIB and name in PYPI_IMPORT_MAP)

def compile_project(server_id: int, path: Path, language: str, startup_file: str | None = None) -> tuple[bool, str]:
    language = normalize_language(language)
    if language == "php":
        php = shutil.which("php")
        if not php:
            return False, "Runtime unavailable: php is not installed"
        version = subprocess.run([php, "-r", "echo PHP_VERSION;"], capture_output=True, text=True, timeout=10).stdout.strip()
        modules = subprocess.run([php, "-m"], capture_output=True, text=True, timeout=10).stdout.splitlines()
        append_log(server_id, "system", f"[PHP] Runtime: PHP {version or 'unknown'}")
        append_log(server_id, "system", f"[PHP] cURL: {'available' if 'curl' in {m.lower() for m in modules} else 'fallback enabled'}")
        entry = first_file_case_insensitive(path, (startup_file,) if startup_file else ("index.php", "server.php", "app.php", "main.php"))
        if not entry:
            return False, "No PHP entry point found (expected index.php)"
        bootstrap = Path(__file__).resolve().parent / "runtime" / "php_bootstrap.php"
        lint_cmd = [php, "-d", f"auto_prepend_file={bootstrap}", "-l", str(entry)] if bootstrap.is_file() else [php, "-l", str(entry)]
        result = subprocess.run(lint_cmd, cwd=path, capture_output=True, text=True, timeout=30)
        append_log(server_id, "stdout", result.stdout)
        append_log(server_id, "stderr", result.stderr)
        if result.returncode == 0:
            append_log(server_id, "system", "[PHP] Syntax check: OK")
            return True, "PHP syntax check passed"
        append_log(server_id, "stderr", "[PHP] Syntax check: FAILED")
        return False, "PHP syntax error; check console logs"
    if language not in {"c", "cpp"}: return True, "Build not required"
    compiler_name = "gcc" if language == "c" else "g++"
    compiler = shutil.which(compiler_name)
    if not compiler: return False, f"{language.upper()} compiler ({compiler_name}) is not installed."
    suffix = "*.c" if language == "c" else "*.cpp"
    sources = sorted(str(x.name) for x in path.glob(suffix))
    if not sources: return False, f"No {language.upper()} source files found"
    command = [compiler, "-O2", *sources, "-o", ".host_build"]
    append_log(server_id, "system", "Compiling: " + " ".join(command))
    result = subprocess.run(command, cwd=path, capture_output=True, text=True, timeout=300)
    append_log(server_id, "stdout", result.stdout)
    append_log(server_id, "stderr", result.stderr)
    return (result.returncode == 0, "Build completed" if result.returncode == 0 else "Compilation failed; check console logs")


def install_dependencies_for_server(server_id: int, path: Path, language: str) -> tuple[bool, str]:
    """Install declared dependencies once, inside the server's own directory."""
    language = normalize_language(language)
    if language == "image": return True, "Image Hosting does not require a process"
    required = {"python":"python3", "nodejs":"node", "php":"php", "java":"java", "ruby":"ruby", "go":"go", "rust":"cargo", "elixir":"mix", "deno":"deno", "shell":"bash", "c":"gcc", "cpp":"g++"}
    if language in required and not shutil.which(required[language]): return False, f"Runtime unavailable: {required[language]} is not installed"
    if language == "php" and (path / "composer.json").is_file() and not (path / "vendor" / "autoload.php").is_file():
        composer = shutil.which("composer")
        if not composer:
            append_log(server_id, "stderr", "[PHP] composer.json found but Composer is not installed")
            return False, "Composer is required by composer.json but is not installed"
        append_log(server_id, "system", "[PHP] Installing Composer dependencies...")
        try:
            result = subprocess.run([composer, "install", "--no-interaction", "--no-progress", "--prefer-dist"], cwd=path, capture_output=True, text=True, timeout=900)
            append_log(server_id, "installation", result.stdout)
            if result.returncode != 0:
                append_log(server_id, "stderr", result.stderr)
                return False, "Composer installation failed; check console logs"
        except (OSError, subprocess.TimeoutExpired) as exc:
            append_log(server_id, "stderr", str(exc))
            return False, "Composer installation timed out or failed"
    if language == "python" and not (path / "requirements.txt").is_file():
        inferred = infer_python_requirements(path)
        if inferred:
            (path / "requirements.txt").write_text("\n".join(inferred) + "\n", encoding="utf-8")
            append_log(server_id, "system", "Generated requirements.txt: " + ", ".join(inferred))
    if language == "python" and (path / "requirements.txt").is_file():
        if (path / "requirements.txt").stat().st_size > 5 * 1024 * 1024:
            return False, "requirements.txt is too large"
        venv_python = path / ".venv" / "bin" / "python"
        if venv_python.is_file():
            smoke = subprocess.run([str(venv_python), "-c", "print('PYTHON_ENV_OK')"], capture_output=True, text=True, timeout=15)
            if smoke.returncode == 0:
                return True, "Python environment already installed"
            append_log(server_id, "stderr", smoke.stderr or smoke.stdout)
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
            smoke = subprocess.run([str(venv_python), "-c", "print('PYTHON_ENV_OK')"], capture_output=True, text=True, timeout=15)
            if smoke.returncode != 0:
                append_log(server_id, "stderr", smoke.stderr or smoke.stdout)
                return False, "Python environment smoke test failed"
            append_log(server_id, "system", "Python dependencies installed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            append_log(server_id, "stderr", str(exc))
            return False, "Dependency installation timed out or failed"
    elif normalize_language(language) == "nodejs" and (path / "package.json").is_file() and not (path / "node_modules").is_dir() and node_dependencies_declared(path):
        if not shutil.which("npm"):
            append_log(server_id, "stderr", "Node.js: Available\nnpm: Missing\nCannot install package.json dependencies without npm.")
            return False, "Node.js is available but npm is missing"
        append_log(server_id, "system", "Installing Node.js dependencies automatically...")
        try:
            npm_command = ["npm", "ci", "--no-audit", "--no-fund"] if (path / "package-lock.json").is_file() else ["npm", "install", "--no-audit", "--no-fund"]
            result = subprocess.run(npm_command, cwd=path, capture_output=True, text=True, timeout=600)
            append_log(server_id, "stdout", result.stdout)
            if result.returncode != 0:
                append_log(server_id, "stderr", result.stderr)
                return False, "Dependency installation failed; check console logs"
        except (OSError, subprocess.TimeoutExpired) as exc:
            append_log(server_id, "stderr", str(exc))
            return False, "Dependency installation timed out or failed"
    return True, "Dependencies ready"

def dependencies_ready(path: Path, language: str) -> bool:
    language = normalize_language(language)
    if language == "python" and (path / "requirements.txt").is_file():
        return (path / ".venv" / "bin" / "python").is_file()
    if language == "nodejs" and (path / "package.json").is_file():
        return not node_dependencies_declared(path) or (path / "node_modules").is_dir()
    if language == "php" and (path / "composer.json").is_file():
        return (path / "vendor" / "autoload.php").is_file()
    return True

def node_dependencies_declared(path: Path) -> bool:
    package_file = path / "package.json"
    if not package_file.is_file():
        return False
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return bool(package.get("dependencies") or package.get("devDependencies"))


def _start_process(user: dict[str, Any], srv: dict[str, Any]) -> tuple[bool, str]:
    if not subscription_active(user):
        return False, "Subscription is inactive or expired"
    if normalize_language(srv.get("language")) == "image":
        with db() as conn: conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("RUNNING", now_iso(), srv["id"]))
        return True, "Image Hosting is ready"
    with process_lock:
        old = processes.get(srv["id"])
        if old and old.poll() is None:
            return False, "Server is already running"
    path = Path(srv["path"])
    if not dependencies_ready(path, srv["language"]):
        message = "Dependencies are not installed; run the installation job and wait for completion before starting"
        with db() as conn:
            conn.execute("UPDATE servers SET status='ERROR',updated_at=? WHERE id=?", (now_iso(), srv["id"]))
        append_log(srv["id"], "stderr", message)
        return False, message
    with db() as conn:
        conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("STARTING", now_iso(), srv["id"]))
    # Dependency installation and compilation are always dispatched by the
    # request layer.  Keep only the cheap PHP lint in the start path; a start
    # request must never block on pip/npm/compiler work.
    language = normalize_language(srv.get("language"))
    php_mode = normalize_php_mode(srv.get("runtime_mode")) if language == "php" else ""
    if language == "php" and php_mode == "webhook":
        public_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not public_url:
            try: public_url = str(get_setting("website_domain", "") or "").strip().rstrip("/")
            except Exception: public_url = ""
        if not public_url.lower().startswith("https://"):
            with db() as conn:
                conn.execute("UPDATE servers SET status='ERROR',updated_at=? WHERE id=?", (now_iso(), srv["id"]))
            append_log(srv["id"], "stderr", "[PHP] Webhook startup refused: configure a public HTTPS domain first")
            return False, "PHP Webhook mode requires a public HTTPS URL"
    if language == "php":
        ready, dependency_message = compile_project(srv["id"], path, language, srv.get("startup_file"))
        if not ready:
            with db() as conn:
                conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
            return False, dependency_message
    if language in {"c", "cpp", "go", "rust"} and not (path / ".host_build").is_file():
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
        return False, "Build is not complete; run the installation/build job first"
    try:
        cmd = runtime_command(srv)
    except (RuntimeError, ValueError) as exc:
        append_log(srv["id"], "stderr", str(exc))
        with db() as conn: conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
        return False, str(exc)
    if not cmd or any(not isinstance(x, str) or not x for x in cmd):
        with db() as conn:
            conn.execute("UPDATE servers SET status='ERROR',updated_at=? WHERE id=?", (now_iso(), srv["id"]))
        append_log(srv["id"], "stderr", "No valid startup command could be determined")
        return False, "No valid startup file or build output found"
    out = path / "console.log"
    public_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_url:
        try: public_url = str(get_setting("website_domain", "") or "").strip().rstrip("/")
        except Exception: public_url = ""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(path), "PORT": str(srv.get("port") or 0), "SERVER_PORT": str(srv.get("port") or 0), "PLATFORM_PUBLIC_URL": public_url, "PUBLIC_URL": public_url}
    # Never pass host secrets or the full host environment to user workloads.
    env.update({k: v for k, v in os.environ.items() if k.startswith("LANG")})
    try:
        proc = subprocess.Popen(cmd, cwd=path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                bufsize=1, env=env, preexec_fn=preexec_for(user, srv.get("language", "")))
    except Exception as exc:
        append_log(srv["id"], "system", f"Failed to start: {exc}")
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("ERROR", now_iso(), srv["id"]))
        return False, str(exc)
    with process_lock:
        processes[srv["id"]] = proc
    # Attach readers before the startup check. Short-lived syntax/import
    # failures must not lose their stderr before the process is reaped.
    threading.Thread(target=pipe_reader, args=(srv["id"], proc.stdout, "stdout"), daemon=True).start()
    threading.Thread(target=pipe_reader, args=(srv["id"], proc.stderr, "stderr"), daemon=True).start()
    threading.Thread(target=reap_process, args=(srv["id"], proc), daemon=True).start()
    time.sleep(float(os.environ.get("STARTUP_CHECK_SECONDS", "0.35")))
    if proc.poll() is not None:
        if proc.returncode == 0 and str(srv.get("server_type", "bot")).lower() == "bot":
            append_log(srv["id"], "system", "CLI project completed during startup check with exit code 0")
            with db() as conn: conn.execute("UPDATE servers SET status='STOPPED',pid=NULL,exit_code=0,updated_at=? WHERE id=?", (now_iso(), srv["id"]))
            return True, "CLI project completed successfully"
        append_log(srv["id"], "stderr", f"Startup check failed: process exited with code {proc.returncode}")
        with db() as conn: conn.execute("UPDATE servers SET status=?,pid=NULL,exit_code=?,updated_at=? WHERE id=?", ("ERROR", proc.returncode, now_iso(), srv["id"]))
        return False, "Process exited during startup; see Console"
    with db() as conn:
        conn.execute("UPDATE servers SET status=?,pid=?,start_time=?,updated_at=? WHERE id=?", ("RUNNING", proc.pid, time.time(), now_iso(), srv["id"]))
        append_log(srv["id"], "system", f"Started PID {proc.pid}: {' '.join(cmd)}")
    if language == "php":
        mode = normalize_php_mode(srv.get("runtime_mode")) or ("website" if srv.get("server_type") == "website" else "cli")
        append_log(srv["id"], "system", f"[PHP] Mode: {mode.upper()} | Entry: {srv.get('startup_file') or 'auto-detected'}")
    return True, "Server started"


def start_process(user: dict[str, Any], srv: dict[str, Any]) -> tuple[bool, str]:
    server_id = int(srv["id"])
    with process_lock:
        if server_id in start_reservations:
            return False, "Server start is already in progress"
        start_reservations.add(server_id)
    try:
        return _start_process(user, srv)
    finally:
        with process_lock:
            start_reservations.discard(server_id)


def stop_process(srv: dict[str, Any], kill: bool = False) -> tuple[bool, str]:
    with process_lock:
        proc = processes.get(srv["id"])
    if not proc or proc.poll() is not None:
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,pid=NULL,updated_at=? WHERE id=?", ("STOPPED", now_iso(), srv["id"]))
        return True, "Server is stopped"
    with db() as conn:
        conn.execute("UPDATE servers SET status='STOPPING',updated_at=? WHERE id=?", (now_iso(), srv["id"]))
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

def invalidate_storage(path: Path) -> None:
    with _usage_cache_lock:
        _usage_cache.pop(str(path.resolve()), None)


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
    if get_setting("maintenance", "0") == "1" and request.path.startswith("/api/") and request.path not in {"/api/csrf", "/api/login", "/api/logout", "/api/current_user", "/api/maintenance-status"}:
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

@app.route("/static/login-hero.png")
def login_hero():
    return send_from_directory(BASE_DIR, "login-hero.png", mimetype="image/png")

@app.route("/static/mobile-neon.css")
def mobile_neon_css():
    return send_from_directory(BASE_DIR, "mobile-neon.css", mimetype="text/css")


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
    password=str(d.get("password", "")); unlimited=bool(d.get("unlimited", False))
    days=int(d.get("subscription_days", d.get("expiry_days", 30)))
    if not password or days < 1 or (not unlimited and days > 365): return jsonify(success=False,message="Password and duration are required"),400
    role=d.get("role", "Administrator" if d.get("is_admin") else "User"); account="Administrator" if role in {"Owner","Administrator","Manager","Support"} else "User"
    max_servers=UNLIMITED_MAX_SERVERS if unlimited else max(1,int(d.get("max_servers",3)))
    storage_value=UNLIMITED_STORAGE if unlimited else int(d.get("storage_per_server",d.get("storage_limit",1024**3)))
    ram_value=UNLIMITED_RAM if unlimited else int(d.get("ram_per_server",512*1024**2))
    cpu_value=UNLIMITED_CPU if unlimited else float(d.get("cpu_per_server",1))
    backups_value=UNLIMITED_BACKUPS if unlimited else max(0,int(d.get("max_backups",3)))
    expiry=(datetime.now(timezone.utc)+timedelta(days=(36500 if unlimited else days))).isoformat()
    try:
        with db() as conn:
            cur=conn.execute("INSERT INTO users(username,password_hash,account_type,role,status,subscription_started_at,subscription_expires_at,max_servers,storage_per_server,storage_limit,storage_used,ram_per_server,cpu_per_server,max_backups,account_expiry,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (username,generate_password_hash(password,method="scrypt"),account,role,"ACTIVE",now_iso(),expiry,max_servers,storage_value,storage_value,0,ram_value,cpu_value,backups_value,expiry,now_iso()))
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
            item=dict(r); owner_row = conn.execute("SELECT * FROM users WHERE id=?", (item["owner_id"],)).fetchone(); owner_data=dict(owner_row) if owner_row else user; item.update(folder=str(item["id"]), title=item["name"], plan="basic", server_type=item.get("server_type") or ("website" if item["language"] == "html" else "bot"), server_slug=item.get("server_slug") or item["slug"], server_language=normalize_language(item.get("server_language") or item["language"]), website_url=item.get("website_url") or "", storage_limit=storage_limit_for(owner_data), storage_used=used_bytes(Path(item["path"])) if Path(item["path"]).exists() else 0, ram_limit=owner_data["ram_per_server"]//(1024**2), cpu_limit=owner_data["cpu_per_server"], is_admin=user["account_type"] == "Administrator")
            item["status"] = "Running" if item["status"] == "RUNNING" else item["status"].title()
            rows.append(item)
    used=sum(used_bytes(Path(r["path"])) for r in rows if Path(r["path"]).exists())
    return jsonify(success=True,servers=rows,stats={"used":len(rows),"total":user["max_servers"],"disk_used":used/(1024**2),"disk_total":storage_limit_for(user)/(1024**2),"expiry":user["subscription_expires_at"],"storage_used":used,"storage_limit":storage_limit_for(user),"storage_remaining":max(0,storage_limit_for(user)-used)})


def detect_language(path: Path) -> str:
    markers = [("package.json","nodejs"),("requirements.txt","python"),("pyproject.toml","python"),("pipfile","python"),("index.php","php"),("composer.json","php"),("pom.xml","java"),("build.gradle","java"),("gradlew","java"),("gemfile","ruby"),("go.mod","go"),("cargo.toml","rust"),("mix.exs","elixir"),("deno.json","deno"),("deno.jsonc","deno"),("index.html","html")]
    names = {entry.name.lower(): entry for entry in path.iterdir()} if path.is_dir() else {}
    for marker, language in markers:
        if names.get(marker.lower()) and names[marker.lower()].is_file(): return language
    if any(path.glob("*.sh")): return "shell"
    if any(path.glob("*.cpp")) or any(path.glob("*.cc")) or any(path.glob("*.cxx")): return "cpp"
    if any(path.glob("*.c")): return "c"
    if any(path.glob("*.py")): return "python"
    if any(path.glob("*.java")): return "java"
    if any(path.glob("*.rb")): return "ruby"
    if any(path.glob("*.go")): return "go"
    if any(path.glob("*.rs")): return "rust"
    if any(path.glob("*.ex")) or any(path.glob("*.exs")): return "elixir"
    if any(path.glob("*.ts")) or any(path.glob("*.js")): return "deno" if shutil.which("deno") and not (path / "package.json").exists() else "nodejs"
    return "python"

def runtime_metadata(path: Path, requested: str = "") -> dict[str, Any]:
    """Describe what can actually run; this is used by upload/create flows."""
    language = normalize_language(requested) if requested else detect_language(path)
    if language not in VALID_LANGUAGES:
        language = detect_language(path)
    commands = {
        "python": "python3", "nodejs": "node", "php": "php", "java": "java",
        "ruby": "ruby", "go": "go", "rust": "cargo", "elixir": "mix",
        "deno": "deno", "shell": "bash", "c": "gcc", "cpp": "g++",
    }
    executable = commands.get(language)
    probe = runtime_probe(language) if language in {"python", "nodejs", "php"} else {}
    available = bool(probe.get("available")) if probe else (language in {"html", "image"} or bool(executable and shutil.which(executable)))
    candidates = []
    for pattern in (("main.py", "app.py", "bot.py", "server.py", "index.py") if language == "python" else
                    ("index.js", "server.js", "app.js", "main.js") if language == "nodejs" else
                    ("index.php",) if language == "php" else ("index.html",) if language == "html" else ("main.rb", "app.rb", "server.rb") if language == "ruby" else ()):
        match = first_file_case_insensitive(path, (pattern,))
        if match: candidates.append(str(match.relative_to(path)))
    entry = candidates[0] if candidates else None
    version = probe.get("version") if probe else None
    if executable and available and not version:
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5)
            version = (result.stdout or result.stderr).splitlines()[0][:120] if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    result = {"language": language, "label": LANGUAGE_LABELS.get(language, language.upper()), "available": available,
            "version": version, "entry_point": entry, "entry_points": candidates,
            "executable": probe.get("executable") if probe else (shutil.which(executable) if executable else None),
            "test_output": probe.get("test_output") if probe else None,
            "message": "Runtime unavailable" if not available else "Runtime available"}
    if language == "php":
        result["project_modes"] = detect_php_project(path)
        result["php_extensions"] = probe.get("php_extensions", {})
        result["php_cgi_executable"] = probe.get("php_cgi_executable")
    if language == "nodejs":
        result["npm_available"] = bool(probe.get("npm_available"))
        result["npm_executable"] = probe.get("npm_executable")
    if probe.get("reason"): result["reason"] = probe["reason"]
    return result

@app.route("/api/runtimes")
def runtimes():
    user, err = require_user()
    if err: return err
    rows = []
    for language in sorted(VALID_LANGUAGES):
        if language in {"node", "javascript"}: continue
        rows.append(runtime_metadata(BASE_DIR, language))
    return jsonify(success=True, runtimes=rows)

@app.route("/api/server/add", methods=["POST"])
def add_server():
    user, err = require_user()
    if err: return err
    if not subscription_active(user): return jsonify(success=False,message="Subscription expired or inactive"),403
    d=json_body(); name=str(d.get("name","")).strip(); lang=normalize_language(d.get("language",d.get("type","python")))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}",name) or lang not in VALID_LANGUAGES: return jsonify(success=False,message="Use a unique slug with a-z, 0-9 and hyphens"),400
    runtime = runtime_metadata(BASE_DIR, lang)
    if not runtime["available"]:
        return jsonify(success=False, message=f"{runtime['label']} runtime unavailable: {runtime.get('reason', 'install the runtime before creating this server')}"), 400
    php_mode = normalize_php_mode(d.get("runtime_mode") or d.get("php_mode") or d.get("mode")) if lang == "php" else ""
    php_detection = {"suggested_mode": "cli", "scores": {"cli": 0, "webhook": 0, "website": 0}, "signals": [], "modes": ["cli", "webhook", "website"]} if lang == "php" else {"suggested_mode": ""}
    if lang == "php" and not php_mode:
        php_mode = php_detection["suggested_mode"]
    if lang == "php" and php_mode not in {"cli", "webhook", "website"}:
        return jsonify(success=False, message="Invalid PHP mode; choose cli, webhook, or website", php_modes=["cli", "webhook", "website"]), 400
    server_type = "image" if str(d.get("type", "")).lower() in {"image", "image_hosting"} or lang == "image" else ("website" if str(d.get("type", "")).lower() == "website" or lang == "html" or php_mode in {"webhook", "website"} else "bot")
    with db() as conn:
        count=conn.execute("SELECT COUNT(*) FROM servers WHERE owner_id=?",(user["id"],)).fetchone()[0]
        if count>=user["max_servers"]: return jsonify(success=False,message="Maximum server limit reached"),403
        slug=name.lower();
        if conn.execute("SELECT 1 FROM servers WHERE slug=?", (slug,)).fetchone(): return jsonify(success=False,message="اسم الخادم مستخدم بالفعل، اختر اسمًا آخر"),409
        cur=conn.execute("INSERT INTO servers(owner_id,name,slug,language,path,status,port,server_type,server_slug,server_language,website_url,last_activity,runtime_mode,webhook_path,php_suggested_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(user["id"],name,slug,lang,"", "CREATED",allocate_port(),server_type,slug,lang,"",now_iso(),php_mode or None,str(d.get("webhook_path") or "/webhook") if php_mode == "webhook" else None,php_detection.get("suggested_mode") or None,now_iso(),now_iso())); sid=cur.lastrowid
        path=USERS_DIR/f"user_{user['id']}"/"servers"/f"{sid}_{slug}"; path.mkdir(parents=True); conn.execute("UPDATE servers SET path=? WHERE id=?",(str(path),sid))
    website_base = get_setting("website_domain", "").strip().rstrip("/")
    website_url = (website_base + f"/site/{slug}") if server_type == "website" and website_base else (f"/site/{slug}" if server_type == "website" else "")
    with db() as conn: conn.execute("UPDATE servers SET website_url=? WHERE id=?", (website_url, sid))
    audit(user["id"],"create_server",str(sid),{"name":name,"language":lang,"type":server_type,"runtime_mode":php_mode or None}); return jsonify(success=True,server={"id":sid,"name":name,"slug":slug,"server_type":server_type,"language":lang,"runtime_mode":php_mode or None,"php_suggested_mode":php_detection.get("suggested_mode"),"website_url":website_url,"status":"CREATED"})


@app.route("/api/server/action/<int:server_id>/<action>", methods=["POST"])
def server_action(server_id:int,action:str):
    user, err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False,message="Server not found"),404
    if action in {"start","restart"}:
        if action=="restart": stop_process(srv)
        path = Path(srv["path"])
        lang = normalize_language(srv.get("language"))
        needs_build = lang in {"c", "cpp"} and not (path / ".host_build").is_file()
        if not dependencies_ready(path, lang) or needs_build:
            job_id, message = queue_install_job(user, srv, auto_start=True)
            audit(user["id"], action, str(server_id), {"job_id": job_id, "queued": True})
            return jsonify(success=True, status="INSTALLING", job_id=job_id, message=message), 202
        ok,msg=start_process(user,srv)
    elif action in {"stop","kill"}: ok,msg=stop_process(srv,action=="kill")
    elif action=="delete":
        stop_process(srv,True)
        with db() as conn: conn.execute("DELETE FROM servers WHERE id=?",(server_id,))
        shutil.rmtree(srv["path"],ignore_errors=True); invalidate_storage(Path(srv["path"])); ok,msg=True,"Server deleted"
    else:return jsonify(success=False,message="Unknown action"),400
    audit(user["id"],action,str(server_id)); return jsonify(success=ok,message=msg)


def read_log_tail(path: Path, limit: int = 100000) -> str:
    if not path.is_file(): return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END); size=f.tell(); f.seek(max(0, size-limit)); data=f.read()
    return data.decode("utf-8", "replace")

def read_log_lines(path: Path, max_lines: int = 1000) -> list[str]:
    if not path.is_file():
        return []
    from collections import deque
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=max_lines))

@app.route("/api/server/stats/<int:server_id>")
def server_stats(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    p=psutil.Process(srv["pid"]) if srv.get("pid") and psutil.pid_exists(srv["pid"]) else None
    return jsonify(success=True,status=("Running" if srv["status"] == "RUNNING" else srv["status"].title()),pid=srv.get("pid"),uptime=(time.time()-srv["start_time"] if srv.get("start_time") else 0),cpu=(p.cpu_percent(0.1) if p else 0),ram=(p.memory_info().rss if p else 0),mem=(f"{(p.memory_info().rss/(1024**2)):.1f} MB" if p else "0 MB"),storage=used_bytes(Path(srv["path"])),logs=read_log_tail(Path(srv["path"])/"console.log"))

@app.route("/api/server/detect/<int:server_id>")
def server_detect(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    return jsonify(success=True, detection=runtime_metadata(Path(srv["path"]), srv.get("language", "")))


def safe_name(value: str) -> str:
    value = str(value or "").replace("\\", "/").split("/")[-1].strip()
    if not value or value in {".", ".."} or any(ord(ch) < 32 for ch in value): raise ValueError("Invalid file name")
    return value

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
        files.append({"name":p.name,"path":str(p.relative_to(Path(srv["path"]))),"is_dir":p.is_dir(),"size":p.stat().st_size if p.is_file() else 0,"modified":p.stat().st_mtime,"is_zip":p.is_file() and p.suffix.lower()==".zip"})
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

@app.route("/api/files/download/<int:server_id>/<path:filename>")
def file_download(server_id, filename):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    target = file_response(srv, filename)
    if not target.exists(): return jsonify(success=False, message="File not found"), 404
    if target.is_file():
        return send_file(target, as_attachment=True, download_name=target.name)
    # Stream a temporary in-memory archive for folders; never expose paths.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for child in target.rglob("*"):
            if child.is_file() and child.name != "console.log":
                z.write(child, child.relative_to(target))
    archive.seek(0)
    return send_file(archive, as_attachment=True, download_name=f"{target.name}.zip", mimetype="application/zip")


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
    dest=file_response(srv,rel)
    uploaded = request.files.getlist("file") or request.files.getlist("files") or request.files.getlist("files[]")
    if not uploaded or all(not (f.filename or "").strip() for f in uploaded):
        return jsonify(success=False,message="No files were selected"),400
    pending=[]; total=0; used_names=set()
    for f in uploaded:
        name=safe_name(f.filename or "")
        if not name: continue
        data=f.read(); total += len(data)
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate=name; index=1
        while candidate in used_names or (dest / candidate).exists():
            candidate=f"{stem} ({index}){suffix}"; index += 1
        used_names.add(candidate); pending.append((candidate,data))
    if not pending:
        return jsonify(success=False,message="No valid files were uploaded"),400
    if user_storage(user["id"], refresh=True) + total > storage_limit_for(user):
        return jsonify(success=False,message="Storage quota exceeded; no files were uploaded"),413
    created=[]; made_dest=False
    try:
        if not dest.exists(): dest.mkdir(parents=True,exist_ok=False); made_dest=True
        if not dest.is_dir(): return jsonify(success=False,message="Upload destination is not a directory"),400
        for name,data in pending:
            target=safe_child(dest,name)
            temp=target.with_name(f".{target.name}.upload-{secrets.token_hex(8)}")
            temp.write_bytes(data); temp.replace(target); created.append(target)
    except (OSError, ValueError) as exc:
        for target in created: target.unlink(missing_ok=True)
        if made_dest: shutil.rmtree(dest,ignore_errors=True)
        return jsonify(success=False,message=f"Upload failed; no files were saved: {exc}"),500
    invalidate_storage(Path(srv["path"]))
    saved=[name for name,_ in pending]
    audit(user["id"],"upload",str(server_id),saved); return jsonify(success=True,files=saved,message="Files uploaded successfully")


@app.route("/api/files/create/<int:server_id>",methods=["POST"])
def file_create(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); d=json_body(); name=safe_name(str(d.get("name") or d.get("filename") or ""))
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
    invalidate_storage(Path(srv["path"]))
    audit(user["id"],"create_file",str(server_id),str(p.relative_to(Path(srv["path"]))))
    return jsonify(success=True)


@app.route("/api/files/delete/<int:server_id>",methods=["POST"])
def file_delete(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); payload=json_body(); names=payload.get("files") or payload.get("names") or ([payload.get("name")] if payload.get("name") else [])
    if isinstance(names, str): names=[names]
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    if not srv:return jsonify(success=False),404
    for name in names:
        p=file_response(srv,str(name))
        if p == Path(srv["path"]): return jsonify(success=False,message="Cannot delete server root"),400
        if p.is_dir():shutil.rmtree(p)
        elif p.is_file():p.unlink()
    invalidate_storage(Path(srv["path"]))
    audit(user["id"],"delete_file",str(server_id),names); return jsonify(success=True)


@app.route("/api/files/rename/<int:server_id>",methods=["POST"])
def file_rename(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); d=json_body(); old=str(d.get("old") or d.get("from") or d.get("old_name") or "")
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    new=safe_name(str(d.get("new") or d.get("to") or d.get("new_name") or ""))
    if not srv or not old or not new:return jsonify(success=False,message="Invalid rename request"),400
    src=file_response(srv,old); dst=file_response(srv,str(Path(old).parent/new))
    if not src.exists() or dst.exists(): return jsonify(success=False,message="Source missing or destination exists"),409
    src.rename(dst); invalidate_storage(Path(srv["path"])); audit(user["id"],"rename_file",str(server_id),{"old":old,"new":new}); return jsonify(success=True)


@app.route("/api/files/unzip/<int:server_id>/<path:filename>",methods=["POST"])
def unzip_api(server_id,filename):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id); src=file_response(srv,filename) if srv else None
    if not subscription_active(user): return jsonify(success=False,message="انتهت مدة حسابك، يرجى التواصل مع الإدارة لتجديد الحساب"),403
    if not src or not src.is_file() or not zipfile.is_zipfile(src):return jsonify(success=False,message="Invalid ZIP"),400
    root=Path(srv["path"])
    try:
        with zipfile.ZipFile(src) as z:
            infos=z.infolist()
            if len(infos)>10000: return jsonify(success=False,message="ZIP contains too many files"),413
            total=sum(max(0, i.file_size) for i in infos)
            if total>512*1024*1024 or user_storage(user["id"], refresh=True)+total>storage_limit_for(user):
                return jsonify(success=False,message="Storage quota exceeded or ZIP is too large"),413
            planned=[]
            for info in infos:
                if info.filename.startswith("/") or ".." in Path(info.filename).parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    return jsonify(success=False,message="Unsafe ZIP entry"),400
                target=safe_child(root,info.filename)
                if target.exists() or target in planned:
                    return jsonify(success=False,message=f"ZIP would overwrite an existing file: {info.filename}"),409
                planned.append(target)
            created=[]
            for info in infos:
                target=safe_child(root,info.filename)
                if info.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(info) as inp, target.open("wb") as out:
                    shutil.copyfileobj(inp,out,1024*1024)
                created.append(target)
    except (zipfile.BadZipFile, OSError) as exc:
        for target in locals().get("created", []): target.unlink(missing_ok=True)
        append_log(server_id,"stderr",f"ZIP extraction failed: {exc}")
        return jsonify(success=False,message="Invalid or damaged ZIP"),400
    invalidate_storage(Path(srv["path"]))
    audit(user["id"],"extract_zip",filename); return jsonify(success=True,message="ZIP extracted successfully")


@app.route("/api/images/<int:server_id>", methods=["GET", "POST"])
def images_api(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv or normalize_language(srv.get("language")) != "image": return jsonify(success=False, message="Image Hosting server not found"), 404
    root = Path(srv["path"]) / "images"; root.mkdir(parents=True, exist_ok=True)
    if request.method == "GET":
        with db() as conn: rows=[dict(r) for r in conn.execute("SELECT * FROM images WHERE server_id=? ORDER BY id DESC", (server_id,))]
        for row in rows: row["url"] = f"/images/{row['storage_name']}"
        return jsonify(success=True, images=rows, storage_used=sum(r["size"] for r in rows), storage_limit=storage_limit_for(user))
    uploads=request.files.getlist("file") or request.files.getlist("files[]")
    if not uploads: return jsonify(success=False, message="No images selected"), 400
    pending=[]; total=0
    for upload in uploads:
        original=secure_filename(upload.filename or "")
        if not original or Path(original).suffix.lower() not in IMAGE_EXTENSIONS: continue
        data=upload.read(); mime=mimetypes.guess_type(original)[0] or ""
        if not mime.startswith("image/") or not valid_image_bytes(data, Path(original).suffix): continue
        total += len(data); pending.append((original,data,mime))
    if not pending: return jsonify(success=False, message="Only safe image files are allowed"),400
    if user_storage(user["id"], refresh=True)+total>storage_limit_for(user): return jsonify(success=False,message="Storage quota exceeded; no images were uploaded"),413
    saved=[]; created=[]
    try:
        with db() as conn:
            for original,data,mime in pending:
                stored=f"{secrets.token_urlsafe(18)}{Path(original).suffix.lower()}"
                target=root/stored; target.write_bytes(data); created.append(target)
                conn.execute("INSERT INTO images(owner_id,server_id,storage_name,original_name,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?)", (user["id"],server_id,stored,original,mime,len(data),now_iso()))
                saved.append({"name":original,"url":f"/images/{stored}"})
    except OSError as exc:
        for target in created: target.unlink(missing_ok=True)
        return jsonify(success=False,message=f"Image upload failed; no images were saved: {exc}"),500
    return jsonify(success=bool(saved), images=saved, message="Images uploaded" if saved else "Only safe image files are allowed"), (200 if saved else 400)

@app.route("/images/<storage_name>")
def image_file(storage_name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|gif|webp)", storage_name, re.I): return "Not found", 404
    with db() as conn: row=conn.execute("SELECT * FROM images WHERE storage_name=?", (storage_name,)).fetchone()
    if not row: return "Not found", 404
    with db() as conn: srv=conn.execute("SELECT path FROM servers WHERE id=?", (row["server_id"],)).fetchone()
    if not srv: return "Not found", 404
    target=Path(srv["path"])/"images"/storage_name
    return send_file(target, mimetype=row["mime_type"]) if target.is_file() else ("Not found",404)

@app.route("/api/images/<int:server_id>/<int:image_id>", methods=["DELETE"])
def image_delete(server_id, image_id):
    user, err = require_user()
    if err: return err
    srv=server_for(user, server_id)
    if not srv: return jsonify(success=False),404
    with db() as conn: row=conn.execute("SELECT * FROM images WHERE id=? AND server_id=?", (image_id,server_id)).fetchone()
    if not row: return jsonify(success=False,message="Image not found"),404
    (Path(srv["path"])/"images"/row["storage_name"]).unlink(missing_ok=True)
    with db() as conn: conn.execute("DELETE FROM images WHERE id=?", (image_id,))
    audit(user["id"],"delete_image",str(image_id)); return jsonify(success=True)

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


@app.route("/api/server/set-runtime-mode/<int:server_id>", methods=["POST"])
def set_runtime_mode(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    if normalize_language(srv.get("language")) != "php":
        return jsonify(success=False, message="Runtime mode selection is currently specific to PHP"), 400
    data = json_body(); mode = normalize_php_mode(data.get("runtime_mode") or data.get("php_mode") or data.get("mode"))
    if mode not in {"cli", "webhook", "website"}:
        return jsonify(success=False, message="Choose cli, webhook, or website", modes=["cli", "webhook", "website"]), 400
    webhook_path = str(data.get("webhook_path") or srv.get("webhook_path") or "/webhook")
    if not webhook_path.startswith("/") or ".." in Path(webhook_path).parts:
        return jsonify(success=False, message="Invalid webhook path"), 400
    server_type = "website" if mode in {"webhook", "website"} else "bot"
    with db() as conn:
        conn.execute("UPDATE servers SET runtime_mode=?,webhook_path=?,server_type=?,updated_at=? WHERE id=?", (mode, webhook_path if mode == "webhook" else None, server_type, now_iso(), server_id))
    audit(user["id"], "set_runtime_mode", str(server_id), {"mode": mode, "webhook_path": webhook_path if mode == "webhook" else None})
    return jsonify(success=True, runtime_mode=mode, server_type=server_type, webhook_path=webhook_path if mode == "webhook" else None)


@app.route("/api/create_api_key", methods=["POST"])
def create_api_key():
    user, err = require_user()
    if err: return err
    raw = "ygh_" + secrets.token_urlsafe(32)
    with db() as conn: conn.execute("UPDATE users SET api_key_hash=? WHERE id=?", (generate_password_hash(raw, method="scrypt"), user["id"]))
    audit(user["id"], "create_api_key")
    return jsonify(success=True, api_key=raw)


@app.route("/api/broadcasts")
def user_broadcasts():
    user, err = require_user()
    if err: return err
    with db() as conn:
        rows=[dict(r) for r in conn.execute("SELECT id,message,audience,created_at FROM broadcasts WHERE audience='all' OR audience=? OR (audience='unbanned' AND ?!='BANNED') ORDER BY id DESC LIMIT 10", ("active" if user.get("status")=="ACTIVE" else "inactive", user.get("status", "")))]
    return jsonify(success=True, broadcasts=rows)

@app.route("/api/maintenance-status")
def maintenance_status():
    return jsonify(success=True, enabled=get_setting("maintenance", "0")=="1", message=get_setting("maintenance_message", "الموقع في وضع الصيانة"))

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


def queue_install_job(user: dict[str, Any], srv: dict[str, Any], auto_start: bool = False) -> tuple[str, str]:
    """Queue dependency/build work and optionally start the server on success."""
    server_id = int(srv["id"])
    with install_jobs_lock:
        active = next((j for j in install_jobs.values() if j["server_id"] == server_id and j["status"] in {"QUEUED", "RUNNING"}), None)
        if active:
            if auto_start:
                active["auto_start"] = True
            return active["id"], "Installation already in progress"
        job_id = secrets.token_urlsafe(12)
        install_jobs[job_id] = {"id": job_id, "server_id": server_id, "status": "QUEUED", "message": "Installation queued", "created_at": now_iso(), "auto_start": auto_start}
    with db() as conn:
        conn.execute("UPDATE servers SET status='INSTALLING',updated_at=? WHERE id=?", (now_iso(), server_id))

    def worker() -> None:
        with install_jobs_lock:
            install_jobs[job_id]["status"] = "RUNNING"
        path = Path(srv["path"]); lang = normalize_language(srv["language"])
        try:
            if lang == "php" and not shutil.which("php"):
                ok, msg = False, "PHP runtime is not installed."
            elif lang in {"c", "cpp"}:
                ok, msg = compile_project(server_id, path, lang)
            elif lang == "html":
                append_log(server_id, "system", "HTML projects do not require dependency installation.")
                ok, msg = True, "HTML static site is ready"
            else:
                ok, msg = install_dependencies_for_server(server_id, path, lang)
        except Exception as exc:
            logging.exception("Install job %s failed", job_id)
            ok, msg = False, f"Installation failed: {exc}"
            append_log(server_id, "stderr", msg)
        with db() as conn:
            conn.execute("UPDATE servers SET status=?,updated_at=? WHERE id=?", ("CREATED" if ok else "ERROR", now_iso(), server_id))
        append_log(server_id, "system", msg)
        with install_jobs_lock:
            should_start = bool(install_jobs.get(job_id, {}).get("auto_start"))
        if ok and should_start:
            with db() as conn:
                user_row = conn.execute("SELECT * FROM users WHERE id=?", (srv["owner_id"],)).fetchone()
                server_row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            if user_row and server_row:
                started, start_message = start_process(dict(user_row), dict(server_row))
                append_log(server_id, "system", f"Queued start {'succeeded' if started else 'failed'}: {start_message}")
                if not started:
                    ok, msg = False, f"{msg}; auto-start failed: {start_message}"
        with install_jobs_lock:
            install_jobs[job_id].update(status="COMPLETED" if ok else "FAILED", message=msg, finished_at=now_iso())

    threading.Thread(target=worker, daemon=True, name=f"install-{server_id}").start()
    return job_id, "Installation queued"


@app.route("/api/server/install/<int:server_id>",methods=["POST"])
def install(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    job_id, message = queue_install_job(user, srv)
    with install_jobs_lock:
        status = install_jobs.get(job_id, {}).get("status", "QUEUED")
    return jsonify(success=True, job_id=job_id, status=status, message=message), 202

@app.route("/api/server/install-status/<job_id>")
def install_status(job_id):
    user, err = require_user()
    if err: return err
    with install_jobs_lock:
        job = dict(install_jobs.get(job_id, {}))
    if not job: return jsonify(success=False, message="Installation job not found"), 404
    srv = server_for(user, int(job["server_id"]))
    if not srv: return jsonify(success=False, message="Installation job not found"), 404
    return jsonify(success=True, job=job)


@app.route("/api/server/console/<int:server_id>/clear", methods=["POST"])
def clear_console(server_id):
    user, err = require_user()
    if err: return err
    srv = server_for(user, server_id)
    if not srv: return jsonify(success=False, message="Server not found"), 404
    (Path(srv["path"]) / "console.log").unlink(missing_ok=True)
    audit(user["id"], "clear_console", str(server_id)); notify(server_id)
    return jsonify(success=True, message="Console cleared")

@app.route("/api/server/console/<int:server_id>")
def console(server_id):
    user,err=require_user();
    if err:return err
    srv=server_for(user,server_id)
    if not srv:return jsonify(success=False),404
    lines=[]; log=Path(srv["path"])/"console.log"
    for line in read_log_lines(log, 1000):
        try: lines.append(json.loads(line))
        except: lines.append({"ts":now_iso(),"stream":"system","line":line.rstrip("\n")})
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
