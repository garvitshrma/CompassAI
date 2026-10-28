"""
navigator/apps.py — the app's configuration object.

Every Django app has one of these. Ours is almost empty on purpose: the obvious
thing to put here is the startup warm-up, and boot.py explains at length why
that belongs in wsgi.py instead (short version: ready() also runs for
`manage.py check`, `shell` and `migrate`, and we do not want those loading a
90MB model or failing on a missing API key).

So this class exists only to name the app and set the default primary-key type —
the latter purely to silence a Django system check warning that appears once
models are added in step 2.
"""

from django.apps import AppConfig


class NavigatorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "navigator"
    verbose_name = "Compass navigator"
