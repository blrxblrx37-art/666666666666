# ArenaHost — Web Hosting Platform

A full-stack hosting / PaaS web application built with **Next.js 16 (App Router)**, **PostgreSQL + Drizzle ORM**, **Tailwind CSS**, and **Monaco Editor**. Users can register, upload a project (ZIP or files), auto-detect its runtime, and manage the full project lifecycle — all from the browser.

---

## Features

- **Authentication** — Email/password with bcrypt + httpOnly session cookies. Google OAuth hooks ready in `.env`.
- **User dashboard** — Overview, projects, console, files, metrics, env vars, domains, backups, settings.
- **Admin panel** — Users, servers, plans, runtimes, activity, audit log, system settings. Role-based (`USER`, `ADMIN`, `SUPER_ADMIN`).
- **Project wizard** — 4-step create flow: details → upload → runtime detection → review & deploy.
- **Runtime auto-detect** — Node, Python, PHP, Go, Rust, Ruby, Java, Static HTML. Reads `package.json`, `requirements.txt`, `composer.json`, etc.
- **ZIP upload & extraction** — Safe extractor with ZIP-slip / zip-bomb / absolute-path / size / count protections.
- **File manager** — Tree navigation, rename, delete, create, upload, edit with Monaco (dark theme, language detection).
- **Deployment pipeline** — Scanning → Installing → Building → Starting → Running, with per-stage progress and real log rows persisted to Postgres.
- **Live console** — Polls new logs by `sinceId`, pause/resume/clear/download, auto-scroll.
- **Metrics** — CPU/RAM/Storage sampled every 10s with Recharts graphs.
- **Env variables** — Add/edit/delete, mask secrets, reveal on demand.
- **Domains & SSL** — Add custom hostnames, primary flag, SSL status indicator.
- **Backups** — Create snapshots of all files, restore in one click, delete, size tracking.
- **Plans & limits** — Admin-editable plans enforce server count, storage, RAM, CPU, backups, and domains.
- **Notifications & activity logs** — Separate user activity + admin audit log.
- **PWA** — `manifest.json` + minimal `sw.js` (shell cache only; API/dashboard are never cached).
- **i18n & themes** — EN / AR toggle with RTL support, dark/light theme toggle, persisted in localStorage.

## Bot hosting (Telegram & WhatsApp — Python / Node.js / PHP)

Projects are executed for real by the process manager in `src/lib/deploy.ts`:

| Template | Runtime | How it connects |
|---|---|---|
| Telegram · Python | `pyTelegramBotAPI` (pip) | long polling, `BOT_TOKEN` |
| Telegram · Node.js | `telegraf` (npm) | long polling, `BOT_TOKEN` |
| Telegram · PHP | cURL, no deps | long polling, `BOT_TOKEN` |
| WhatsApp · Node.js | `@whiskeysockets/baileys` | QR code (rendered in the Console) or pairing code via `WA_PHONE` |
| WhatsApp · Python | stdlib `http.server` | official Cloud API webhook at `/apps/<id>/webhook` |
| WhatsApp · PHP | `php -S` router | official Cloud API webhook at `/apps/<id>/webhook` |

Pipeline: files synced from DB → workspace (`.data/servers/<id>`, symlink-safe) → real `pip install --target .packages` /
`npm install` / `composer install` (cached by manifest hash) → spawn start command **without a shell**
(allow-listed executables, shell operators rejected) → health check → RUNNING.
stdout/stderr stream into the Console (stdin input supported), secrets are redacted from logs, CPU/RAM are read from
`/proc` every 10s, the plan RAM limit is enforced (OOM kill), crashes auto-restart with back-off and a crash-loop guard
(5 crashes / 2 min). Running projects are resumed after a platform restart; orphans are killed.

Public gateway: `/apps/<serverId>/…` serves static sites and reverse-proxies to a project's `$PORT`
(sandbox CSP + Set-Cookie stripping isolate user content from the platform origin).

## Architecture

```
Browser (React / Next.js App Router)
    │
    │ HTTPS
    ▼
Next.js API routes (REST)
    │
    ├── Auth / Session (bcrypt + httpOnly cookie)
    ├── Server lifecycle manager  ──► Deployment stages + log pipeline
    ├── File store                ──► Postgres `files` table
    ├── Metrics sampler
    └── Admin RBAC + Audit log
    │
    ▼
PostgreSQL (Drizzle ORM)
```

> **Container runtime note:** The architecture is designed so each project runs inside an isolated container (CPU/RAM/storage/PID limits, dropped Linux caps, non-root, no privileged mode, no Docker-socket mount). The preview sandbox does **not** expose a Docker socket to this process, so `src/lib/deploy.ts` currently simulates the container stage transitions while writing real log rows and real metrics to Postgres. In production (VPS/Linux), replace the `runStages` body with Docker SDK calls — the DB contract and UI stay identical.

## Tech Stack

- **Framework**: Next.js 16 (App Router, Node runtime)
- **DB**: PostgreSQL via `drizzle-orm` + `pg`
- **UI**: Tailwind CSS v4, Lucide icons, Recharts, Monaco Editor
- **Validation**: Zod
- **Security**: bcrypt (12 rounds), SHA-256 session token hashing in DB, parameterized queries only, path normalization + ZIP-slip protection

## Running locally

```bash
cp .env.example .env   # edit DATABASE_URL
npm install
npx drizzle-kit push   # applies the schema
npm run dev
```

First boot auto-seeds:
- 4 default plans (`free`, `basic`, `pro`, `business`)
- 7 runtimes (Node, Python, PHP, Static, Go, etc.)
- A **super-admin** account using `ADMIN_EMAIL` / `ADMIN_PASSWORD` from `.env`

The super-admin account is created automatically on boot from `ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env`
(only if that account does not exist yet — an existing password is never overwritten).
On an empty database the schema is created automatically from `./drizzle` migrations.

## Project layout

```
src/
├─ app/
│  ├─ api/                 ← REST endpoints
│  │  ├─ auth/             login / register / logout / me
│  │  ├─ servers/[id]/     files, env, logs, metrics, backups, domains, action
│  │  ├─ admin/            users, servers, plans, runtimes, settings, overview
│  │  ├─ dashboard/overview
│  │  ├─ detect/           runtime detector
│  │  └─ extract-zip/
│  ├─ dashboard/           user-facing UI (requires auth)
│  ├─ admin/               admin-only UI
│  ├─ login/  register/    auth pages
│  └─ page.tsx             marketing landing page
├─ components/             shared UI primitives (Button, Card, Modal, Toast, ...)
├─ db/                     Drizzle schema & client
└─ lib/                    auth, deploy pipeline, runtime detect, zip, bootstrap
```

## Security hardening in place

- Bcrypt passwords, SHA-256 of session tokens stored in DB (never raw)
- httpOnly, SameSite=Lax, Secure (in production) cookies
- All API routes verify auth; mutations verify **ownership** server-side
- Admin APIs verify role; super-admin required for role changes
- All DB access via Drizzle (parameterized) — no SQL string concatenation
- ZIP: 50MB input / 150MB extracted / 5000 entries / 10MB per-file caps, blocks absolute paths, strips common root wrapper, rejects `..` traversal
- File manager normalizes every path and rejects `..`, null bytes, segments > 255 chars
- Secret env vars are masked by default; reveal is explicit
- Admin audit log tracks every admin mutation
- CSP-friendly, no `eval`, no `exec`, no `shell=True`-style helpers

## Known sandbox limitations

- **No Docker socket in preview** — container start/stop is simulated via DB state transitions. All UI, logs, lifecycle hooks and metrics are real; swapping in Docker SDK is isolated to `src/lib/deploy.ts`.
- **Metrics** — sampled with deterministic values when `RUNNING` (preview only). The DB table, chart, retention + API are production-ready.
- **Google OAuth** — endpoints and settings toggle are in place; wire the actual callback once `GOOGLE_CLIENT_ID/SECRET` are set.
- **SSL / Let's Encrypt** — hostnames are tracked with an `sslStatus` field; ACME wiring is left to the Nginx/Caddy layer on your VPS.

See `.env.example` for all configurable environment variables.
