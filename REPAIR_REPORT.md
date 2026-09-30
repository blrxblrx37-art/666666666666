# Repair and Verification Report

## Scope

The project was audited and repaired in place without deleting users, servers, files, settings, authentication data, or UI files.

## Files modified

- `app.py`
- `REPAIR_REPORT.md`
- `README.md`
- `data/host.db` was updated by the additive startup migration to re-anchor archived server paths and repair orphaned audit-log actor references. No user/server/file records were deleted.

## Repairs implemented

### Final lifecycle pass (2026-09-30)

- Fixed the start path so missing Python/Node dependencies and C/C++ build artifacts are queued through the existing background-job manager instead of being installed or compiled inside the HTTP request.
- Added automatic start after a queued preparation job succeeds, while preserving the existing `/api/server/action/<id>/start` route and returning `202` with the job ID.
- Concurrent manual installation and start requests now share one job; a start request upgrades the active job to auto-start when needed.
- Job completion is published only after the auto-start callback finishes, so polling `install-status` cannot report `COMPLETED` while the server is still being started.

- Added atomic per-server start reservations to prevent duplicate concurrent starts.
- Added explicit `STARTING`, `STOPPING`, `RUNNING`, `CRASHED`, and `ERROR` transitions for process lifecycle operations.
- Added graceful process-stop transition before SIGTERM/SIGKILL handling.
- Added auto-restart with exponential backoff and crash-loop protection: five crashes in ten minutes pauses automatic restarts and marks the server `ERROR`.
- Added bounded `console.log` rotation controlled by `MAX_LOG_BYTES` (10 MiB default).
- Changed console reads to bounded tail reads instead of loading the whole log file.
- Moved dependency installation and C/C++ builds to background jobs.
  - `POST /api/server/install/<id>` returns `202` and a `job_id`.
  - `GET /api/server/install-status/<job_id>` reports job progress and the real failure message.
- Expanded language detection to include Java, Ruby, Go, Rust, Elixir, and source-file fallbacks.
- Added cached executable probes with real smoke tests for Python, Node.js/npm, and PHP; the API now reports executable paths, versions, and test output.
- PHP entry-point detection now supports `index.php`, `server.php`, `app.php`, and `main.php`.
- Node startup now honors `package.json` `scripts.start`; without it, it falls back through `index.js`, `server.js`, `app.js`, `main.js`, and `bot.js` instead of blindly running `npm start`.
- Node dependency installation uses `npm ci` when `package-lock.json` exists and avoids duplicate installs when a Start request arrives during an install job.
- One-shot CLI projects that exit with code 0 during startup are reported as successful completion rather than a crash; non-zero exits remain server errors.
- Server creation rejects runtimes that fail the actual host probe.
- Added `GET /api/runtimes`, based on actual executables detected in the host environment.
- Ensured failed startup-command detection records `ERROR` rather than leaving a server stuck in `STARTING`.
- Stopped printing a new unused admin password on every restart.
- Preserved existing path-traversal, ownership, CSRF, ZIP-slip, and subprocess-without-shell protections.

## Tests executed

- Python backend syntax: passed (`python3 -m py_compile app.py`).
- Frontend JavaScript syntax: passed for `login.html`, `index.html`, and `admin_panel.html` using Node `--check`.
- Flask import and route registration: passed; 62 routes loaded.
- Registration and login: passed.
- Session and CSRF-protected API calls: passed.
- Server creation: passed.
- File upload: passed.
- Path traversal attempt: rejected with JSON `400`.
- Real Python server startup: passed and emitted `PYTHON_OK`.
- Duplicate start: rejected without creating a second process.
- Graceful stop: passed.
- Asynchronous install job: queued, completed, and returned `Dependencies ready`.
- Runtime availability endpoint: passed and reports unavailable runtimes truthfully.
- Cross-user file access: rejected with `404`.
- ZIP Slip extraction: rejected with JSON `400` and `Unsafe ZIP entry`.
- Unknown API route: returns JSON `404` rather than HTML.
- SQLite `integrity_check`: `ok`.
- SQLite `foreign_key_check`: no violations after additive cleanup.
- Static security scan: no `shell=True`, `os.system`, or `extractall` in `app.py`.
- Available runtime smoke tests:
  - Python: `PYTHON_OK`
  - Node.js: `NODE_OK`
  - Shell: `SHELL_OK`
  - C: `C_OK`
  - C++: `CPP_OK`

- PHP runtime smoke tests:
  - `php -v`: PHP 8.3.6
  - `php --version`: PHP 8.3.6
  - `php -r "echo 'PHP_RUNTIME_OK';"`: passed
  - temporary PHP script: `PHP_TEST_OK`
- PHP CLI project: `PHP_CLI_OK`, exit code 0, successful one-shot completion.
- PHP Web project: `PHP_WEB_OK` served from the PHP built-in server on an allocated port; Stop passed.
- Python bot: `PYTHON_BOT_OK`; Start, Console, Stop, and Restart passed.
- Node bot with `package.json` but no `scripts.start`: `NODE_BOT_OK`; fallback to `index.js`, install job, Start, Console, and Stop passed.

### Post-patch verification

- `python3 -m py_compile app.py`: passed.
- Flask import and route registration: passed; 63 routes loaded.
- SQLite integrity and foreign-key checks: passed (`ok`, zero violations).
- Frontend JavaScript syntax: passed for `login.html`, `index.html`, `admin_panel.html`, and `landing.html`.
- Isolated async lifecycle: passed; Start returned `202`, queued Python environment preparation, auto-started after completion, emitted `ASYNC_START_OK`, and Stop returned success.
- PHP host runtime installed and verified: PHP 8.3.6 CLI, PHP 8.3.6 CGI, Composer 2.7.1, and `curl`, `mbstring`, `xml`, `zip`, and `intl` extensions.
- PHP CLI smoke test: `PHP_RUNTIME_OK`.
- PHP built-in web-server smoke test: `PHP_WEB_OK` over HTTP.
- Runtime Manager PHP probe: available, executable `/usr/bin/php`, syntax lint and CLI/web command generation passed.
- PHP mode suite: passed for explicit CLI Bot, Webhook server, and Website server modes; PHP source containing `setWebhook` ran as CLI when CLI mode was selected.
- PHP project detector: returns `suggested_mode`, scores, evidence signals, and selectable modes instead of forcing or rejecting a mode.
- PHP Composer path: passed with a real `composer install` against a minimal `composer.json` and generated `vendor/autoload.php`.
- PHP Webhook safety gate: correctly refuses startup without a public HTTPS base URL; CLI mode remains unrestricted.
- Runtime regression suite: Python duplicate-start/stop, Node start/stop, PHP syntax failure, PHP port conflict, and platform survival all passed.

### Final streaming and durability pass (2026-09-30)

- File and image uploads now copy bounded chunks from the upload stream to server-local temporary files, enforce the remaining quota while writing, validate image signatures from a bounded prefix, and atomically rename completed files into place. Failed uploads remove all temporary data.
- Folder downloads and backups now build ZIP archives on disk with ZIP64 support rather than using `io.BytesIO()`. Temporary folder-download archives are removed after the response is finalized.
- Folder archiving skips symlinks and verifies resolved paths remain inside the requested server directory.
- Port allocation is serialized and performs a real bind check before returning a port, reducing duplicate reservations under concurrent requests.
- SSE log reads are capped to 64 KiB per iteration so a large append cannot block or duplicate the entire log in memory.
- PHP website/webhook slugs proxy HTTP requests to the running PHP built-in server, preserving method, query, headers, and body instead of returning PHP source code.
- Installation jobs are persisted in SQLite with start/finish timestamps, status, exit code, error, and restart interruption handling. Previously queued/running jobs are marked failed on platform restart rather than disappearing.
- Server rows now persist exit reason, crash time, and automatic restart count.

### Final sandbox verification

- `python3 -m py_compile app.py`: passed.
- Embedded JavaScript syntax for `login.html`, `index.html`, `admin_panel.html`, and `landing.html`: passed.
- Static security scan: no `file.read()`, `io.BytesIO()`, `shell=True`, or `extractall()` in `app.py`.
- Isolated upload/backup/download suite: passed with a 2 MiB upload, nested folder ZIP download, backup creation, runtime listing, and SQLite integrity/foreign-key checks.
- Isolated Python lifecycle suite: passed; process started, emitted console output, stopped gracefully, and persisted `STOPPED`/exit metadata.
- Current sandbox runtime detection reports Python, Node.js/npm, Java runtime, Shell, C, C++, HTML, and Image Hosting available; PHP, `javac`, Ruby, Go, Rust/Cargo, Deno, and Elixir/Mix are unavailable in this environment and are not claimed as fully supported.

### Automatic archive preparation pass (2026-09-30)

- Uploads ending in `.zip`, `.tar`, `.tar.gz`, or `.tgz` now return a preparation job and are extracted automatically in a background thread; the existing manual unzip route remains available.
- ZIP and TAR extraction validates member paths, rejects absolute paths, traversal, symlinks, hard links, special files, excessive entry counts, and excessive uncompressed size. Extraction is streamed to disk.
- A single top-level directory is normalized as the project root to avoid unnecessary nested folders. Archives with multiple top-level items use the extraction root.
- After extraction, the analyzer detects Python, PHP, or Node.js from project markers and common files, records the entrypoint, writes analyzer/runtime log stages, and queues dependency preparation automatically.
- The frontend polls the preparation job and refreshes the file list after extraction, while existing upload fields and manual extraction controls remain compatible.
- Unsupported projects fail with a clear preparation error and are not started.

### Automatic archive integration tests

- Python ZIP project: upload, automatic extraction, root flattening, analysis, dependency preparation, start, console output, and stop passed.
- Malicious ZIP containing `../escape.py`: asynchronous job failed safely and created no outside file.
- Node.js ZIP project with `package.json` and `index.js`: extraction, analysis, dependency preparation, start, console output, and stop passed.

## Runtime matrix in this sandbox

| Runtime | Observed status | Notes |
|---|---|---|
| Python | Available | Python 3.12.3 |
| Node.js | Available | v22.13.0; npm available |
| PHP | Available | PHP 8.3.6 CLI/CGI, Composer 2.7.1, required extensions verified |
| Java | Java runtime available | OpenJDK 21.0.12.1; `javac` is not installed, so Java source compilation cannot be guaranteed |
| Ruby | Not installed | Correctly reported unavailable |
| Go | Not installed | Correctly reported unavailable |
| Rust/Cargo | Not installed | Correctly reported unavailable |
| Elixir/Mix | Not installed | Correctly reported unavailable |
| Deno | Not installed | Correctly reported unavailable |
| Shell | Available | Bash 5.2.21 |
| C | Available | GCC 13.3.0 |
| C++ | Available | G++ 13.3.0 |
| HTML/static | Available | Process-free website hosting |
| Image hosting | Available | Process-free authenticated storage flow |

## Remaining deployment limitations

- This sandbox does not provide Docker/container isolation. The existing process limits, reduced environment, ownership checks, and per-server directories improve safety, but production-grade hostile multi-tenant isolation requires containers or dedicated worker hosts.
- Java source builds require a JDK compiler (`javac`) or a supplied prebuilt JAR; only the Java runtime was present during testing.
- PHP was installed safely after checking that `sudo` and `apt-get` were available, then validated with CLI, CGI, Composer, SQLite, and Web tests. Ruby, Go, Rust, Elixir, and Deno remain unavailable and are rejected at server creation instead of being claimed as supported.
- The background-job registry is process-local. For multi-worker production deployment, persist jobs in SQLite/Redis or run a dedicated worker service so jobs survive web-worker restarts.
- The existing frontend visual design and features were preserved; the create-server modal now adds an explicit PHP mode selector without removing existing controls.
