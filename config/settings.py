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
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'relay',
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
    'DIRS': [],
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
MEDIA_URL = '/attachments/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'attachments')
ASGI_APPLICATION = 'config.asgi.application'

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                         'NAME': str(BASE_DIR / 'db.sqlite3')}}

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

DOPPLER_RELAY = {
    "API_KEY": env("DOPPLER_RELAY_API_KEY", default=""),
    "ACCOUNT_ID": env.int("DOPPLER_RELAY_ACCOUNT_ID", default=0),
    "AUTH_SCHEME": env("DOPPLER_RELAY_AUTH_SCHEME", default="Bearer"),
    "BASE_URL": env("DOPPLER_RELAY_BASE_URL", default="https://api.dopplerrelay.com/"),
    "TIMEOUT": 30,
    "DEFAULT_FROM_EMAIL": env("DOPPLER_RELAY_FROM_EMAIL", default=""),
    "DEFAULT_FROM_NAME": env("DOPPLER_RELAY_FROM_NAME", default=""),
}
