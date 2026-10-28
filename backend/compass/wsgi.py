"""
compass/wsgi.py — what the production server actually imports.

WSGI vs ASGI, and why this port uses WSGI
-----------------------------------------
uvicorn spoke ASGI, the async protocol. Django speaks both, and we choose WSGI
here for a specific reason: every view in the old api.py was a plain `def`, not
an `async def`. Not one of them awaited anything. So the async machinery was
never actually being used — the FastAPI docstring's claim that "ASGI matters for
an I/O-bound service" was true in principle but not exercised by this code.

Running sync views under WSGI with a thread pool gives the same concurrency
behaviour with a simpler stack. If a future endpoint genuinely needs async,
Django supports `async def` views under ASGI and you would swap this for
asgi.py and run uvicorn again.

 read more:
   https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
   https://asgi.readthedocs.io/en/latest/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compass.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

# ---- the startup hook ------------------------------------------------------
# This line is the direct stand-in for FastAPI's `lifespan`. It runs when the
# server process imports this module and never during a management command,
# which is precisely the behaviour we want (see the long note in boot.py).
#
# It is placed AFTER get_wsgi_application() deliberately: that call is what
# initialises Django's app registry and settings, and boot.warm() imports our
# own modules, which expect a configured Django.
from compass.boot import warm  # noqa: E402

warm()
