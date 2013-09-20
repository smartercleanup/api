from os import environ

DEBUG = True
TEMPLATE_DEBUG = DEBUG
SHOW_DEBUG_TOOLBAR = DEBUG

ADMINS = (
    # ('Your Name', 'your_email@example.com'),
)

MANAGERS = ADMINS

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.', # Add 'postgresql_psycopg2', 'mysql', 'sqlite3' or 'oracle'.
        'NAME': '',                      # Or path to database file if using sqlite3.
        'USER': '',                      # Not used with sqlite3.
        'PASSWORD': '',                  # Not used with sqlite3.
        'HOST': '',                      # Set to empty string for localhost. Not used with sqlite3.
        'PORT': '',                      # Set to empty string for default. Not used with sqlite3.
    }
}

###############################################################################
#
# Server Configuration
#

# Hosts/domain names that are valid for this site; required if DEBUG is False
# See https://docs.djangoproject.com/en/1.5/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ['*']
SECRET_KEY = 'pbv(g=%7$$4rzvl88e24etn57-%n0uw-@y*=7ak422_3!zrc9+'
SITE_ID = 1

# How long to keep api cache values. Since the api will invalidate the cache
# automatically when appropriate, this can (and should) be set to something
# large.
API_CACHE_TIMEOUT = 604800  # a week

###############################################################################
#
# Time Zones
#

TIME_ZONE = 'Universal'
USE_TZ = True

###############################################################################
#
# Internationalization and Localization
#

LANGUAGE_CODE = 'en-us'
USE_I18N = True
USE_L10N = True

###############################################################################
#
# Templates and Static Assets
#

MEDIA_ROOT = ''
MEDIA_URL = ''

STATIC_ROOT = ''
STATIC_URL = '/static/'
STATICFILES_FINDERS = (
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
)
STATICFILES_DIRS = ()

TEMPLATE_LOADERS = (
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
)
TEMPLATE_DIRS = ()

TEMPLATE_CONTEXT_PROCESSORS = (
    "django.contrib.auth.context_processors.auth",
    "django.core.context_processors.debug",
    "django.core.context_processors.i18n",
    "django.core.context_processors.media",
    "django.core.context_processors.static",
    "django.core.context_processors.tz",
    "django.contrib.messages.context_processors.messages",
    'django.core.context_processors.request',
)

###############################################################################
#
# Request/Response processing
#

WSGI_APPLICATION = 'project.wsgi.application'
ROOT_URLCONF = 'project.urls'

MIDDLEWARE_CLASSES = (
    'django.middleware.common.CommonMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Uncomment the next line for simple clickjacking protection:
    # 'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'debug_toolbar.middleware.DebugToolbarMiddleware',

    'sa_api_v2.middleware.RequestTimeLogger',
)

###############################################################################
#
# Pluggable Applications
#

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.sites',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Uncomment the next line to enable the admin:
    'django.contrib.admin',
    # Uncomment the next line to enable admin documentation:
    # 'django.contrib.admindocs',
    'django.contrib.gis',

    # 3rd-party reusaple apps
    'djangorestframework',
    'rest_framework',
    'south',
    'django_nose',
    'debug_toolbar',
    'storages',

    # Project apps
    'beta_signup',
    'sa_api_v2',
    'sa_api_v2.apikey',
    'sa_api_v1',
    'sa_api_v1.apikey_v1',
    'sa_manager',
)

################################################################################
#
# Testing and administration
#

# Tests (nose)
TEST_RUNNER = 'django_nose.NoseTestSuiteRunner'
SOUTH_TESTS_MIGRATE = True

# Debug toolbar
def custom_show_toolbar(request):
    return SHOW_DEBUG_TOOLBAR
DEBUG_TOOLBAR_CONFIG = {
    'SHOW_TOOLBAR_CALLBACK': custom_show_toolbar,
    'INTERCEPT_REDIRECTS': False
}
INTERNAL_IPS = ('127.0.0.1',)

################################################################################
#
# Logging Configuration
#

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse'
        }
    },
    'formatters': {
        'verbose': {
            'format': '%(levelname)s %(asctime)s %(name)s: %(message)s %(process)d %(thread)d'
        },
        'moderate': {
            'format': '%(levelname)s %(asctime)s %(name)s: %(message)s'
        },
        'simple': {
            'format': '%(levelname)s %(message)s'
        },
    },
    'handlers': {
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler'
        },
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'moderate'
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': True,
        },
        'sa_api_v2': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },
        'sa_api_v1': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },
        'sa_manager': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },

        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': True,
        },

        'django.db.backends': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': True,
        },

        'utils.request_timer': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': True,
        },

        'storages': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': True,
        },

        'redis_cache': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': True,
        },
    }
}

##############################################################################
# Environment loading
from urlparse import urlparse

if 'DATABASE_URL' in environ:
    import dj_database_url
    # NOTE: Be sure that your DATABASE_URL has the 'postgis://' scheme.
    DATABASES = {'default': dj_database_url.config()}

if 'DEBUG' in environ:
    DEBUG = (environ['DEBUG'].lower() == 'true')
    TEMPLATE_DEBUG = DEBUG

if 'REDIS_URL' in environ:
    scheme, connstring = environ['REDIS_URL'].split('://')
    userpass, netloc = connstring.split('@')
    userename, password = userpass.split(':')
    CACHES = {
        "default": {
            "BACKEND": "redis_cache.cache.RedisCache",
            "LOCATION": "%s:1" % (netloc,),
            "OPTIONS": {
                "CLIENT_CLASS": "redis_cache.client.DefaultClient",
                "PASSWORD": password,
            }
        }
    }

    SESSION_ENGINE = "django.contrib.sessions.backends.cache"

if all([key in environ for key in ('SHAREABOUTS_AWS_KEY',
                                   'SHAREABOUTS_AWS_SECRET',
                                   'SHAREABOUTS_AWS_BUCKET')]):
    AWS_ACCESS_KEY_ID = environ['SHAREABOUTS_AWS_KEY']
    AWS_SECRET_ACCESS_KEY = environ['SHAREABOUTS_AWS_SECRET']
    AWS_STORAGE_BUCKET_NAME = environ['SHAREABOUTS_AWS_BUCKET']
    AWS_QUERYSTRING_AUTH = False
    AWS_PRELOAD_METADATA = True

    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto.S3BotoStorage'
    ATTACHMENT_STORAGE = DEFAULT_FILE_STORAGE
    STATICFILES_STORAGE = DEFAULT_FILE_STORAGE
    STATIC_URL = 'http://%s.s3.amazonaws.com/' % AWS_STORAGE_BUCKET_NAME

if 'SHAREABOUTS_ADMIN_EMAIL' in environ:
    ADMINS = (
        ('Shareabouts API Admin', environ.get('SHAREABOUTS_ADMIN_EMAIL')),
    )

if 'CONSOLE_LOG_LEVEL' in environ:
    LOGGING['handlers']['console']['level'] = environ.get('CONSOLE_LOG_LEVEL')

##############################################################################
# Local GEOS/GDAL installations (for Heroku)

import os.path

if os.path.exists('/app/.geodjango/geos/lib/libgeos_c.so'):
    GEOS_LIBRARY_PATH = '/app/.geodjango/geos/lib/libgeos_c.so'

if os.path.exists('/app/.geodjango/gdal/lib/libgdal.so'):
    GDAL_LIBRARY_PATH = '/app/.geodjango/gdal/lib/libgdal.so'

##############################################################################
# Local settings overrides
# ------------------------
# Override settings values by importing the local_settings.py module.

try:
    from .local_settings import *
except ImportError:
    pass
