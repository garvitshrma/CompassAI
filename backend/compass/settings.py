"""
===============================================================================
 compass/settings.py — the configuration that FastAPI simply did not have.
===============================================================================

This file has no FastAPI counterpart. In api.py, configuration was scattered:
`FastAPI(title=...)` set up the app, `app.add_middleware(CORSMiddleware, ...)`
configured CORS, and `os.environ.get(...)` was read inline wherever a secret was
needed. Django centralises all of it here and loads it exactly once, before any
of your code runs.

That is the single biggest mental shift in this port: Django is CONFIGURED, then
run. FastAPI is just run.

-------------------------------------------------------------------------------
 WHAT IS DELIBERATELY ABSENT, AND WHY
-------------------------------------------------------------------------------
There is NO DATABASE configured here, and no django.contrib.auth, admin,
sessions or messages. That is not an oversight — it is the point of this step.

We are changing ONE variable (the web framework) while storage stays exactly as
it was: chunks.json + embeddings.npy on disk, loaded into the STATE dict. If we
added Postgres in the same commit and something broke, we would not know which
change broke it.

So Django runs here in "no database" mode. It supports this fine, as long as you
do not install any app that expects a DB. The section marked STEP 2 below is
what you uncomment when we move to Postgres + pgvector, and that is also the
moment the admin panel and auth become available.

 read more:
   settings reference ... https://docs.djangoproject.com/en/5.1/ref/settings/
   DRF settings ......... https://www.django-rest-framework.org/api-guide/settings/
"""

# Same reasoning as the top of the old api.py: load the .env file into
# os.environ BEFORE anything reads a secret. Here it matters even more, because
# this module is evaluated by Django during startup, before any view exists.
from dotenv import load_dotenv
load_dotenv()

import os
from pathlib import Path

# BASE_DIR points at the backend/ folder (this file is
# backend/compass/settings.py, so two .parent hops). `.resolve()` makes it
# absolute, which protects against anything that changes the working directory.
#
# HISTORICAL NOTE, because this comment used to say the opposite:
# before the reorganisation, retrieval.py defined SITES_DIR = Path("sites") — a
# RELATIVE path, resolved against the current working directory rather than
# against the code. That forced the Django project to sit at the repo root,
# because running it from anywhere else would have created a second, empty
# sites/ folder and made every site report "not registered" with nothing
# visibly broken. core/retrieval.py now anchors both SITES_DIR and MODEL_CACHE
# to __file__, so the data location is a fact about the code instead of a fact
# about your shell — which is what made moving into backend/ safe.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---- secrets and environment -----------------------------------------------
# Django refuses to start without a SECRET_KEY. It signs cookies and tokens.
# Nothing in Compass currently uses signing, but Django checks for it anyway, so
# we supply one. The fallback is clearly marked as dev-only; in production Render
# sets the real value as an environment variable.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-change-in-production"
)

# DEBUG must be False in production: with it on, Django returns full stack
# traces (including settings values) in HTTP responses. Note the string
# comparison — environment variables are ALWAYS strings, so `bool(os.environ
# .get("DEBUG"))` would be True even for "False", a classic bug.
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"

# ALLOWED_HOSTS is a Host-header allowlist, Django's defence against HTTP Host
# header poisoning. FastAPI has no equivalent check by default.
# "*" is acceptable here because this service is a public read-mostly API with
# no cookie-based sessions and no password reset emails — the attacks the
# allowlist defends against need one of those to be meaningful.
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

# ---- applications ----------------------------------------------------------
# An "app" is Django's unit of reusable functionality. Each entry is imported at
# startup, and its AppConfig.ready() hook runs — which is how navigator/apps.py
# gets a chance to warm the embedding model.
INSTALLED_APPS = [
    # staticfiles is needed only so DRF's browsable API (our replacement for
    # FastAPI's /docs) can serve its CSS. It needs no database.
    "django.contrib.staticfiles",
    "corsheaders",            # the CORS middleware package
    "rest_framework",         # DRF: serializers, @api_view, Response
    "navigator",              # our own app — the ported endpoints
    # ---- STEP 2 (Postgres + pgvector) will add: ----------------------------
    # "django.contrib.contenttypes",   # required by auth
    # "django.contrib.auth",           # users, permissions
    # "django.contrib.sessions",       # login sessions for the dashboard
    # "django.contrib.messages",       # required by admin
    # "django.contrib.admin",          # the free CRUD dashboard
    # "pgvector.django",               # VectorField + CosineDistance
]

# ---- middleware ------------------------------------------------------------
# Middleware is a chain every request passes through on the way in and every
# response passes through on the way out. It is the direct equivalent of
# `app.add_middleware(...)` in api.py, except the ORDER IS SIGNIFICANT and
# written top-to-bottom.
#
# CorsMiddleware must come as early as possible — specifically before
# CommonMiddleware — because it has to be able to answer a browser's preflight
# OPTIONS request and return immediately, before anything else gets a chance to
# redirect or reject it.
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Not included, because they all require a database and we have none yet:
    #   SessionMiddleware, AuthenticationMiddleware, MessageMiddleware,
    #   CsrfViewMiddleware
    # CSRF deserves a word: it protects form posts made by a logged-in browser
    # session. Compass has no sessions and no cookies, so there is no session for
    # an attacker to ride — the token would add nothing. When the dashboard
    # arrives in step 2 it WILL need CsrfViewMiddleware; the /query and /ingest
    # API endpoints will stay exempt, which is what DRF's APIView does anyway.
]

# ---- CORS ------------------------------------------------------------------
# Identical policy to the old api.py, same reasoning: Compass is embedded on
# customer sites whose domains we cannot know in advance, and /query is a
# read-only endpoint over content that was public already. The write endpoints
# carry their own checks (admin token on /register, Origin check on /ingest).
#  read more: https://github.com/adamchainz/django-cors-headers
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_METHODS = ["GET", "POST", "OPTIONS"]

# ---- URL handling ----------------------------------------------------------
ROOT_URLCONF = "compass.urls"

# THIS ONE LINE PREVENTS A REAL BUG. Django's default APPEND_SLASH=True means a
# request to an unmatched "/query" gets a 301 redirect to "/query/". Browsers
# follow a 301 on a POST by re-issuing it as a GET WITHOUT the body — so the
# widget's POST would silently arrive as an empty GET and fail in a way that is
# genuinely hard to diagnose. Our URLs have no trailing slashes (to stay
# byte-identical to the FastAPI routes the deployed widget already calls), so we
# turn the redirect off entirely.
APPEND_SLASH = False

WSGI_APPLICATION = "compass.wsgi.application"

# ---- database --------------------------------------------------------------
# An empty dict is the documented way to tell Django "this project has no
# database". Any ORM query would now raise, which is a useful guardrail: it
# means nothing can quietly start depending on a DB before we deliberately add
# one in step 2.
DATABASES = {}

# ---- templates -------------------------------------------------------------
# Needed only by DRF's browsable API. APP_DIRS=True lets Django find templates
# that ship inside installed apps, which is where DRF keeps its own.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

STATIC_URL = "static/"

# ---- request body size -----------------------------------------------------
# ANOTHER BUG THAT ONLY A TEST FINDS. Django refuses any request body larger
# than DATA_UPLOAD_MAX_MEMORY_SIZE, which DEFAULTS TO 2.5MB — and
# navigator/views.MAX_INGEST_BYTES allows 3MB. So without this line, a rendered
# page between 2.5MB and 3MB (entirely normal for a content-heavy site) would:
#
#   * have been accepted by FastAPI, which has no such global limit, but
#   * be rejected by Django BEFORE our view runs, with a RequestDataTooBig
#     exception rendered as a 400 HTML error page rather than our clean
#     {"detail": "page too large"} JSON 413.
#
# The widget parses the response as JSON, so it would get a parse error instead
# of a usable message, on a page size that used to work.
#
# We set Django's ceiling slightly ABOVE our own so that OUR check is the one
# that fires and OUR error shape is what clients see. The application-level
# limit stays the real policy; this is just getting the framework out of its way.
#  read more: https://docs.djangoproject.com/en/5.1/ref/settings/#data-upload-max-memory-size
DATA_UPLOAD_MAX_MEMORY_SIZE = 4_000_000   # > MAX_INGEST_BYTES (3_000_000)

# ---- Django REST Framework -------------------------------------------------
# DRF is the layer that replaces what FastAPI gave us for free: request parsing,
# validation, clear 400-level errors, and a browsable interface.
REST_FRAMEWORK = {
    # No authentication and no permission checks by default, matching the old
    # behaviour exactly: /query, /auto-register, /ingest, /site, /ask and
    # /page-query were all open, and /register did its own token check inline.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],

    # THESE TWO LINES ARE NOT OPTIONAL, and they are a good example of why you
    # test a port instead of assuming it works.
    #
    # DRF populates `request.user` on every request. When no authentication
    # class produces a user it falls back to UNAUTHENTICATED_USER, whose default
    # is django.contrib.auth.models.AnonymousUser — and merely IMPORTING that
    # module pulls in django.contrib.contenttypes' models. With auth not in
    # INSTALLED_APPS (we have no database yet), that import blows up with:
    #
    #   RuntimeError: Model class django.contrib.contenttypes.models.ContentType
    #   doesn't declare an explicit app_label and isn't in an application in
    #   INSTALLED_APPS
    #
    # — on EVERY request, as a 500, with a traceback that points at Django
    # internals and mentions nothing about authentication. Setting these to None
    # tells DRF "there is no user concept here", which is the truth today.
    #
    # Step 2 installs django.contrib.auth for the dashboard; at that point both
    # lines can simply be deleted and the defaults become correct again.
    "UNAUTHENTICATED_USER": None,
    "UNAUTHENTICATED_TOKEN": None,
    # JSON only in production. The browsable HTML API is a development
    # convenience — and a memory cost on a 512MB box — so it is DEBUG-only.
    # In DEBUG, visit any endpoint in a browser to get a form you can POST
    # from; that is the nearest thing to FastAPI's /docs.
    "DEFAULT_RENDERER_CLASSES": (
        [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
        if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
}

# ---- logging ---------------------------------------------------------------
# The old code used bare print() calls, which worked because uvicorn did not
# capture stdout. Under gunicorn, print() output can be buffered or swallowed
# depending on configuration, so the ported code uses the logging module
# instead. This config sends our logger's output to the console, which is where
# Render's log viewer reads from.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{levelname}] {name}: {message}", "style": "{"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Our own code logs under this name; see navigator/services.py.
        "compass": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
