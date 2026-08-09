import os
import secrets
from datetime import timedelta

# Fixed folder where users.db always lives, no matter which directory you
# run "python webserver.py" from. Built relative to THIS file (not a
# hardcoded Windows path), so it works unchanged on your laptop, on
# teammates' machines, and on any Linux server you deploy to.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

# Loads authsystem/.env (if it exists) into os.environ BEFORE any
# os.environ.get() call below runs - so putting a key in that file works
# exactly like setting it in the terminal, but it's remembered across
# restarts. See .env.example for the format. Never commit the real .env
# file (it's already listed in .gitignore) - only .env.example is safe
# to commit, since it has no real secrets in it.
try:
    from dotenv import load_dotenv
    # override=True: .env always wins, even if a stale value from an
    # earlier `export`/`set` in this terminal session is still hanging
    # around. Without this, a leftover terminal env var silently beats
    # whatever you just wrote in .env, which is a confusing thing to
    # debug (looks like .env is being ignored).
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except ImportError:
    pass  # python-dotenv not installed - .env is simply skipped; env vars set in the terminal still work fine

DB_PATH = os.path.join(INSTANCE_DIR, "users.db").replace("\\", "/")

# Used only if SECRET_KEY isn't set via env var AND no persisted dev key
# exists yet. Persisting it avoids invalidating every session on each
# restart during local development, while still never hardcoding a real
# secret in source.
_DEV_SECRET_PATH = os.path.join(INSTANCE_DIR, ".dev_secret_key")


def _get_dev_secret_key() -> str:
    if os.path.exists(_DEV_SECRET_PATH):
        with open(_DEV_SECRET_PATH, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(_DEV_SECRET_PATH, "w") as f:
        f.write(key)
    return key


class Config:
    # In production, ALWAYS set SECRET_KEY via an environment variable.
    # Locally, falls back to an auto-generated key persisted in
    # instance/.dev_secret_key (gitignore this file).
    SECRET_KEY = os.environ.get("SECRET_KEY", _get_dev_secret_key())

    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Session / cookie hardening ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # --- "Continue with Google/GitHub" ---
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

    # --- AI Tutor provider (see ai_provider.py) ---
    # Both empty by default -> ai_provider.py falls back to its built-in
    # mock tutor, so the AI Tutor page works out of the box with no key
    # set. Set ONE of these (never both are required) to go live - the
    # matching _call_*() function in ai_provider.py handles it. Nothing
    # else in the app needs to change.
    HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY", "")
    AI_PROVIDER_API_KEY = os.environ.get("AI_PROVIDER_API_KEY", "")  # OpenAI key, if you use that instead

    # --- AI Tutor file uploads + generated images ---
    # Caps the size of any single request body Flask will accept (applies
    # site-wide, not just the AI Tutor) - stops oversized uploads before
    # they hit disk at all.
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB

    AI_TUTOR_TEXT_EXTENSIONS = {
        ".txt", ".md", ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
        ".json", ".csv", ".java", ".c", ".cpp", ".h", ".sql", ".yaml", ".yml",
        ".xml", ".sh", ".log",
    }
    AI_TUTOR_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    AI_TUTOR_MAX_EXTRACTED_CHARS = 6000  # how much of an uploaded text file gets sent to the AI as context