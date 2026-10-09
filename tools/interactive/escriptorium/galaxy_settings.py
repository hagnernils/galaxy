import os

from escriptorium.settings import *  # noqa: F403


if os.environ.get("GALAXY_INTERACTIVE", "").lower() != "true":
    raise RuntimeError("Galaxy settings require GALAXY_INTERACTIVE=true")

WORKSPACE = os.environ["GALAXY_WORKSPACE"]
RUNTIME_DIR = os.environ["GALAXY_RUNTIME_DIR"]
SECRET_KEY = os.environ["SECRET_KEY"]
ROOT_URLCONF = "galaxy_login"
MEDIA_ROOT = os.path.join(RUNTIME_DIR, "media")
STATIC_ROOT = os.path.join(RUNTIME_DIR, "static")
FILE_UPLOAD_TEMP_DIR = os.path.join(RUNTIME_DIR, "tmp")
CELERYD_STATE_DB = os.path.join(RUNTIME_DIR, "tmp", "celery_state.db")
CSRF_TRUSTED_ORIGINS = [origin for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if origin]
SESSION_COOKIE_SECURE = os.environ.get("GALAXY_COOKIE_SECURE", "false").lower() == "true"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

LOGGING["handlers"]["file"]["filename"] = os.path.join(WORKSPACE, "logs", "django-error.log")  # noqa: F405
