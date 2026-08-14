from pathlib import Path
import environ
import os

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(os.path.join(BASE_DIR, '.env'), overwrite=True)

DEBUG = env('DEBUG', default=False)
SECRET_KEY = env('SECRET_KEY')
if not SECRET_KEY.strip():
    raise ImproperlyConfigured('SECRET_KEY must not be empty.')

ALLOWED_HOSTS = [h.strip() for h in env(
    'ALLOWED_HOSTS', default='127.0.0.1,localhost').split(',') if h.strip()]

# Seguridad HTTPS. Los defaults mantienen compatible el desarrollo local por HTTP.
# Produccion debe habilitar explicitamente estas variables detras de un proxy confiable.
SECURE_PROXY_SSL_HEADER_ENABLED = env.bool(
    'SECURE_PROXY_SSL_HEADER_ENABLED', default=False
)
SECURE_PROXY_SSL_HEADER = (
    ('HTTP_X_FORWARDED_PROTO', 'https')
    if SECURE_PROXY_SSL_HEADER_ENABLED
    else None
)
SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=False)
SESSION_COOKIE_SECURE = env.bool('SESSION_COOKIE_SECURE', default=False)
CSRF_COOKIE_SECURE = env.bool('CSRF_COOKIE_SECURE', default=False)
SECURE_HSTS_SECONDS = env.int('SECURE_HSTS_SECONDS', default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    'SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False
)
SECURE_HSTS_PRELOAD = env.bool('SECURE_HSTS_PRELOAD', default=False)

INSTALLED_APPS = [
    'relay_super',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'relay.apps.RelayConfig',
    'reports',
    'templates_admin',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'config.middlewares.HideReportsAdminMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [str(BASE_DIR / 'config' / 'templates')],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]

WSGI_APPLICATION = 'config.wsgi.application'

# Configuración de archivos de media
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR
ASGI_APPLICATION = 'config.asgi.application'

# Base de datos default
# Para desarrollo sin Postgres, habilita USE_SQLITE=1 en .env (por defecto sigue DEBUG)
USE_SQLITE = env.bool('USE_SQLITE', default=bool(env('DEBUG', default=False)))

if USE_SQLITE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'db.sqlite3'),
        }
    }
else:
    # PostgreSQL (mismo servidor por defecto)
    DB_HOST = env('DB_HOST', default='127.0.0.1')
    DB_PORT = env('DB_PORT', default='5432')
    DB_NAME = env('DB_NAME', default='relay_app')
    DB_USER = env('DB_USER', default='relay_user')
    DB_PASSWORD = env('DB_PASSWORD', default='')

    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': DB_HOST,
            'PORT': DB_PORT,
            'NAME': DB_NAME,
            'USER': DB_USER,
            'PASSWORD': DB_PASSWORD,
            'CONN_MAX_AGE': 60,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'es-ec'
TIME_ZONE = 'America/Guayaquil'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = str(BASE_DIR / 'staticfiles')
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Asegurar redirección /admin -> /admin/
APPEND_SLASH = True

DOPPLER_RELAY = {
    "API_KEY": env("DOPPLER_RELAY_API_KEY", default=""),
    "ACCOUNT_ID": env.int("DOPPLER_RELAY_ACCOUNT_ID", default=0),
    "AUTH_SCHEME": env("DOPPLER_RELAY_AUTH_SCHEME", default="Bearer"),
    "BASE_URL": env("DOPPLER_RELAY_BASE_URL", default="https://api.dopplerrelay.com/"),
    "TIMEOUT": 30,
    "DEFAULT_FROM_EMAIL": env("DOPPLER_RELAY_FROM_EMAIL", default=""),
    "DEFAULT_FROM_NAME": env("DOPPLER_RELAY_FROM_NAME", default=""),
}

# Config por defecto para reportería (ajustable por .env via environ.Env si se desea)
DOPPLER_REPORTS = {
    "TIMEOUT": int(env("DOPPLER_REPORTS_TIMEOUT", default=30)),
    "POLL_INITIAL_DELAY": int(env("DOPPLER_REPORTS_POLL_INITIAL_DELAY", default=2)),
    "POLL_MAX_DELAY": int(env("DOPPLER_REPORTS_POLL_MAX_DELAY", default=15)),
    "POLL_TOTAL_TIMEOUT": int(env("DOPPLER_REPORTS_POLL_TOTAL_TIMEOUT", default=15 * 60)),
}

# bulk-v2-real-send-canary (design.md §12.1): additive over Django's own
# DEFAULT_LOGGING. No root key, no django logger override — only a
# dedicated `relay` logger at INFO reaching stderr (journald under systemd).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "relay_kv": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "relay_stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "level": "INFO",
            "formatter": "relay_kv",
        },
    },
    "loggers": {
        "relay": {
            "handlers": ["relay_stderr"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# TD-02A imports a persistent occurrence ledger only. Existing and default
# campaigns remain on the legacy engine.
BULK_PROCESSING_ENGINE_V2 = env.bool(
    "BULK_PROCESSING_ENGINE_V2", default=False
)
BULK_PROCESSING_V2_CANARY_ENABLED = env.bool(
    "BULK_PROCESSING_V2_CANARY_ENABLED", default=False
)
BULK_PROCESSING_V2_CANARY_REQUEST_IDS = env(
    "BULK_PROCESSING_V2_CANARY_REQUEST_IDS", default=""
)
BULK_PROCESSING_V2_CANARY_USER_IDS = env(
    "BULK_PROCESSING_V2_CANARY_USER_IDS", default=""
)
BULK_PROCESSING_V2_CANARY_MAX_ROWS = env.int(
    "BULK_PROCESSING_V2_CANARY_MAX_ROWS", default=20
)

# bulk-v2-real-send-canary (design.md §9): independent real-send authorization
# surface, deliberately separate from the BULK_PROCESSING_V2_CANARY_* flags
# above (canary = import-only; these gate an actual outbound Doppler send).
# All defaults are fail-closed/inert: disabled, empty allowlists, MAX_ROWS=1.
# No flag here is activated by this change. Activating any of them requires
# separate, later, explicit owner authorization outside this SDD change.
BULK_PROCESSING_V2_REAL_SEND_ENABLED = env.bool(
    "BULK_PROCESSING_V2_REAL_SEND_ENABLED", default=False
)
BULK_PROCESSING_V2_REAL_SEND_USER_IDS = env(
    "BULK_PROCESSING_V2_REAL_SEND_USER_IDS", default=""
)
BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS = env(
    "BULK_PROCESSING_V2_REAL_SEND_REQUEST_IDS", default=""
)
BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS = env(
    "BULK_PROCESSING_V2_REAL_SEND_TEMPLATE_IDS", default=""
)
BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS = env(
    "BULK_PROCESSING_V2_REAL_SEND_RECIPIENT_DOMAINS", default=""
)
BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS = env.int(
    "BULK_PROCESSING_V2_REAL_SEND_MAX_ROWS", default=1
)

# bulk-v2 quota guard (design round 5/6, PR A): dormant infrastructure
# settings. Nothing in the send path reads DOPPLER_QUOTA_GUARD_ENABLED yet
# (PR B wires it in) -- these exist only so relay/services/bulk_quota.py
# and its tests have a real settings surface to resolve against. All
# defaults are fail-closed/inert: guard disabled, limits at 0 (invalid,
# never "unlimited" -- see bulk_quota._resolve_effective_limit), no
# safety margin, overage unsupported.
DOPPLER_QUOTA_GUARD_ENABLED = env.bool(
    "DOPPLER_QUOTA_GUARD_ENABLED", default=False
)
DOPPLER_QUOTA_MONTHLY_LIMIT = env.int(
    "DOPPLER_QUOTA_MONTHLY_LIMIT", default=0
)
DOPPLER_QUOTA_DAILY_LIMIT = env.int(
    "DOPPLER_QUOTA_DAILY_LIMIT", default=0
)
DOPPLER_QUOTA_HOURLY_LIMIT = env.int(
    "DOPPLER_QUOTA_HOURLY_LIMIT", default=0
)
DOPPLER_QUOTA_SAFETY_MARGIN_RATIO = env.float(
    "DOPPLER_QUOTA_SAFETY_MARGIN_RATIO", default=0.0
)
DOPPLER_QUOTA_OVERAGE_ENABLED = env.bool(
    "DOPPLER_QUOTA_OVERAGE_ENABLED", default=False
)

BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP = env.bool(
    "BULK_PROCESSING_V2_ALLOW_EXTERNAL_TEMPLATE_LOOKUP", default=False
)
BULK_PROCESSING_V2_IMPORT_BATCH_SIZE = env.int(
    "BULK_PROCESSING_V2_IMPORT_BATCH_SIZE", default=1000
)
BULK_PROCESSING_V2_MAX_FILE_BYTES = env.int(
    "BULK_PROCESSING_V2_MAX_FILE_BYTES", default=50 * 1024 * 1024
)
BULK_PROCESSING_V2_MAX_ROWS = env.int(
    "BULK_PROCESSING_V2_MAX_ROWS", default=250000
)
BULK_PROCESSING_V2_SPOOL_MAX_AGE_SECONDS = env.int(
    "BULK_PROCESSING_V2_SPOOL_MAX_AGE_SECONDS", default=86400
)
