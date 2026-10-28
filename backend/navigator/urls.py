"""
navigator/urls.py — URL to view mapping for the ported endpoints.

Each path is written WITHOUT a trailing slash, matching the FastAPI routes
exactly. That is not a style choice: the widget already deployed on live
customer sites fetches "/query" and "/ingest", and settings.APPEND_SLASH is
switched off, so a mismatch here would be a 404 in production rather than a
forgiving redirect.

`name=` gives each route a reverse-lookup handle, so tests and templates can
refer to a route by name instead of hardcoding its path.
 read more: https://docs.djangoproject.com/en/5.1/topics/http/urls/#reverse-resolution-of-urls
"""

from django.urls import path

from navigator import views

urlpatterns = [
    # ---- status --------------------------------------------------------
    path("health", views.health, name="health"),
    path("sites", views.sites, name="sites"),

    # ---- the widget ----------------------------------------------------
    path("query", views.query, name="query"),
    path("register", views.register, name="register"),
    path("auto-register", views.auto_register, name="auto-register"),
    path("ingest", views.ingest, name="ingest"),

    # ---- the Chrome extension ------------------------------------------
    path("page-query", views.page_query, name="page-query"),
    path("site", views.site, name="site"),
    path("ask", views.ask, name="ask"),

    # ---- static page ---------------------------------------------------
    # A plain Django view, not a DRF one. Django routes both kinds side by side.
    path("privacy", views.privacy, name="privacy"),
]
