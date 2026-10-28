#!/usr/bin/env python
"""
manage.py — Django's command-line entry point.

WHAT THIS REPLACES
------------------
Under FastAPI there was no equivalent file. You ran `uvicorn api:app` and that
was the whole story. Django instead gives you one script that dispatches to
every management command: runserver, shell, migrate, createsuperuser, and any
command you write yourself later.

    python manage.py runserver        # the dev server (replaces uvicorn --reload)
    python manage.py shell            # a REPL with Django already configured
    python manage.py check            # static checks without starting anything

WHY DJANGO_SETTINGS_MODULE EXISTS
---------------------------------
Django has no implicit configuration. Every entry point must say which settings
module to use, by setting this environment variable before touching anything
else. `setdefault` means "use this unless the environment already specified
something else" — which is how you'd point a test run at a different settings
file without editing this file.
"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compass.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Could not import Django. Is it installed and is your virtual "
            "environment active? Try: pip install -r requirements.txt"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
