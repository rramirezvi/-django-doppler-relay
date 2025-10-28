from pathlib import Path
import environ
import os

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

DEBUG = env('DEBUG', default=False)
SECRET_KEY = env('SECRET_KEY', default='unsafe-secret-key')
ALLOWED_HOSTS = [h.strip() for h in env(
    'ALLOWED_HOSTS', default='127.0.0.1,localhost').split(',') if h.strip()]

INSTALLED_APPS = [
    'relay_super',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'relay',
    'reports',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
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
MEDIA_URL = '/'  # La URL base para archivos media
MEDIA_ROOT = BASE_DIR  # La raíz será el directorio base del proyecto
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

