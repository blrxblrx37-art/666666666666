# •-تــيــ۾ إڪـXـس-•

Secure Flask hosting control plane with SQLite persistence, hashed passwords, role-aware administration, per-server quotas, validated file operations, runtime process control, logs, SSE console streaming, and backups.

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

Important environment variables: `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `DATA_DIR`, `USERS_DIR`, `DB_FILE`, `PORT`, `COOKIE_SECURE=1`, and `MAX_UPLOAD_BYTES`.
