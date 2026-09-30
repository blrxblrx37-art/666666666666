# •-تــيــ۾ إڪـXـس-•

Secure Flask hosting control plane with SQLite persistence, hashed passwords, role-aware administration, per-server quotas, validated file operations, runtime process control, logs, SSE console streaming, and backups.

## Implemented platform features

- Runtime catalog: Python, Node.js, PHP, Java, Ruby, Go, Rust, Elixir, Deno, Shell, C, C++, and HTML/Static.
- Runtime availability checks return clear errors instead of starting a known-invalid server.
- PHP uses the built-in server; interpreted runtimes use argv-based subprocesses without `shell=True`.
- Startup validation prevents a process that exits immediately from being shown as `RUNNING`.
- Server-side console clearing deletes the stored log; log responses read only a bounded tail.
- SSE console updates are supported by the existing stream endpoint; the dashboard reduces fallback refresh frequency.
- Image Hosting servers are process-free and include authenticated multi-upload, safe image signatures, per-owner records, deletion, quota checks, and unique direct URLs at `/images/<id>.<ext>`.
- Existing SQLite data is retained and the `images` table is additive.

## Reliability controls

- Dependency installation and C/C++ builds run as background jobs. `POST /api/server/install/<id>` returns `202` with a `job_id`; poll `GET /api/server/install-status/<job_id>` for `QUEUED`, `RUNNING`, `COMPLETED`, or `FAILED`.
- Server starts are serialized per server, transition through `STARTING`, and reject duplicate starts.
- Unexpected exits honor `auto_restart` with exponential backoff and stop after five crashes in ten minutes to prevent infinite restart loops.
- Console logs are rotated at `MAX_LOG_BYTES` (10 MiB by default), and console APIs read bounded tails instead of loading the entire file.
- `GET /api/runtimes` reports runtime availability from real executables in the host environment; unavailable runtimes are not presented as runnable.
- Python, Node.js/npm, and PHP availability includes a cached version check and an actual smoke command (`PYTHON_RUNTIME_OK`, `NODE_RUNTIME_OK`, or `PHP_RUNTIME_OK`). Server creation rejects runtimes that fail these probes.
- Node projects honor `scripts.start`; otherwise the manager falls back to `index.js`, `server.js`, `app.js`, `main.js`, and `bot.js`. `npm ci` is used when a lockfile exists.
- PHP supports CLI projects and built-in web projects separately, with entry-point detection for `index.php`, `server.php`, `app.php`, and `main.php`.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ADMIN_USERNAME='Admin@gmail.com'
export ADMIN_PASSWORD='Use-a-strong-password-here'
python app.py
```

The app creates `data/host.db` on first run. Set `ADMIN_USERNAME` and a strong `ADMIN_PASSWORD` before first production start; if omitted, the app generates a random initial admin password and prints it once. For production, run behind HTTPS with a process supervisor and isolate user workloads in containers or a dedicated worker host; this sandbox does not provide Docker.

Important environment variables: `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `DATA_DIR`, `USERS_DIR`, `DB_FILE`, `PORT`, `COOKIE_SECURE=1`, `MAX_UPLOAD_BYTES`, `STARTUP_CHECK_SECONDS`, `MAX_LOG_BYTES`, and `LOG_LEVEL`.

### PHP host prerequisites

PHP is supported in both CLI bot mode and built-in web-server mode. On Ubuntu/Debian, install the host runtime and common extensions with:

```bash
sudo apt-get install -y php-cli php-cgi php-curl php-mbstring php-xml php-zip composer
```

The runtime manager validates `php` with a real smoke command, runs PHP syntax checks before launch, supports `index.php`, `main.php`, `app.php`, `server.php`, and `bot.php`, and uses Composer when a PHP project declares Composer dependencies.

PHP projects support three explicit modes: `cli` for long-polling/CLI bots, `webhook` for HTTP webhook bots, and `website` for PHP websites/APIs. Detection is advisory and exposed by `GET /api/server/detect/<id>`; it never rejects a project because source code contains `setWebhook` or Telegram keywords. Select or change the mode with `POST /api/server/set-runtime-mode/<id>`. Webhook mode requires a configured public HTTPS domain (`PUBLIC_BASE_URL` or the admin website-domain setting); CLI mode does not. Website and webhook requests are proxied to the running PHP built-in server so PHP source is executed rather than downloaded.

## Compatibility and security

File operations accept existing API field variants (`files`, `names`, and `name`), preserve Unicode filenames, encode nested file URLs safely, expose `is_zip`, and protect ZIP extraction with traversal, symlink, entry-count, size, and quota checks. Large uploads are streamed in bounded chunks to temporary files; folder downloads and backups are assembled on disk rather than in memory. Existing SQLite data and API routes are preserved through additive compatibility logic. All state-changing requests use the session CSRF token, server ownership is checked on server/file/image APIs, and uploaded Image Hosting files must match both a safe extension and the expected binary signature. Installation jobs and process exit metadata are persisted in SQLite, while atomic port bind checks prevent duplicate allocations.

Archive uploads (`.zip`, `.tar`, `.tar.gz`, and `.tgz`) are prepared automatically in the background. The platform validates and extracts them safely, flattens a single project directory when appropriate, detects Python/PHP/Node.js and a common entrypoint, refreshes the file list, and queues dependency preparation. Invalid or unsupported archives fail with a visible job error and are never started.
