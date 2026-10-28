"""
===============================================================================
 compass/boot.py — the replacement for FastAPI's `lifespan`.
===============================================================================

THE PROBLEM THIS SOLVES
-----------------------
api.py had one clean place to do expensive startup work:

    @asynccontextmanager
    async def lifespan(app):
        model()                          # load the 90MB ONNX embedder
        STATE["sites"] = load_all_sites()
        STATE["llm"] = Groq(...)
        yield                            # <- serve requests
        STATE["sites"].clear()           # <- shutdown

Django has no direct equivalent, and the obvious-looking substitute is a trap.

WHY NOT AppConfig.ready()?
--------------------------
`ready()` is Django's documented "app is loaded" hook, so it looks like the
right home for this. But ready() runs for EVERY management command, not just the
web server. So `python manage.py check`, `shell`, `collectstatic` and later
`migrate` would each spend a second loading an embedding model and would each
refuse to run at all if GROQ_API_KEY were unset. Deploy scripts run those
commands. That is how you get a build that fails for a reason unrelated to the
thing it was doing.

THE SHAPE USED HERE
-------------------
A plain idempotent `warm()` function, called from two places:

  1. compass/wsgi.py, at import time. The WSGI module is imported by gunicorn
     and by runserver, and by NOTHING ELSE — so this gives us "run on server
     start, never on a management command", which is exactly what lifespan did.

  2. Defensively at the top of each view, where it is a no-op after the first
     call. This covers the cases wsgi.py cannot: a `manage.py shell` session
     where you want to call the pipeline by hand, and any test that invokes a
     view directly.

The double-checked locking is the same idiom used in the old /auto-register,
and it is here for the same reason: two requests arriving simultaneously on a
cold process must not both start loading a 90MB model.

ON SHUTDOWN
-----------
There is no teardown half. The old `STATE["sites"].clear()` after the yield was
cosmetic — the process is exiting and the OS reclaims the memory regardless. Not
having a shutdown hook costs us nothing here, but it is a real difference worth
knowing if you later add something that genuinely needs closing, like a database
connection pool. For those, Django's answer is a signal handler or the
connection framework rather than a lifespan block.
"""

import logging
import os
import threading

log = logging.getLogger("compass")

_warm_lock = threading.Lock()
_warmed = False


def warm():
    """Load the embedder, the site indexes and the Groq client. Idempotent."""
    global _warmed

    # Fast path, deliberately OUTSIDE the lock. After the first call this
    # function is hit on every single request, and taking a lock each time would
    # serialise all requests through one mutex for no reason. Reading a bool is
    # atomic under CPython, so this is safe.
    if _warmed:
        return

    with _warm_lock:
        # Re-check inside the lock: several threads may have passed the check
        # above before any of them acquired it.
        if _warmed:
            return

        # Imported lazily, inside the function, ON PURPOSE. These modules pull
        # in numpy and fastembed, which are slow to import. Keeping them out of
        # module scope means `manage.py check` and friends stay fast and do not
        # need the heavy dependencies resolved just to validate settings.
        from core.retrieval import load_all_sites, model
        from groq import Groq

        from navigator.state import STATE

        key = os.environ.get("GROQ_API_KEY")
        if not key:
            # Same fail-fast reasoning as the original lifespan: better to
            # refuse to boot than to accept traffic and 500 on the first real
            # query. Raised here, this propagates out of wsgi.py and gunicorn
            # exits with the message visible in the deploy log.
            raise RuntimeError("Set GROQ_API_KEY before starting the server.")

        log.info("warming embedder + loading sites...")

        # Calling model() and discarding the result is the warm-up: it forces
        # the lazy singleton in retrieval.py to load the ONNX model now, on our
        # time, instead of on the first visitor's.
        model()

        STATE["sites"] = load_all_sites()

        # One shared client, so TCP and TLS handshakes are not repeated per
        # request. The timeout and retry values are a latency decision, not
        # boilerplate — see the long note in the original api.py: on a free tier
        # where 429s are routine, the SDK's default max_retries=2 with backoff
        # turns one slow call into three sequential attempts, and the visitor
        # experiences that as the widget hanging.
        STATE["llm"] = Groq(api_key=key, timeout=8.0, max_retries=1)

        _warmed = True
        log.info("ready. %d site(s): %s", len(STATE["sites"]), list(STATE["sites"]))
