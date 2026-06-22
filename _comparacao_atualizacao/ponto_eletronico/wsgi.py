from app import app, ensure_runtime_dirs, init_db

ensure_runtime_dirs()
init_db()

application = app
