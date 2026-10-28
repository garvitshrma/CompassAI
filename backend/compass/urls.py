"""
compass/urls.py — the root URL map.

WHAT THIS REPLACES
------------------
In api.py, a route and its handler were declared together:

    @app.post("/query", response_model=QueryOut)
    def query(body: QueryIn): ...

Django splits that in two: the handler lives in views.py, and the mapping from
URL to handler lives here. The split costs you one extra line per endpoint and
buys you one place where every URL in the project is visible at a glance —
which is worth more than it sounds once there are thirty of them.

Note there is no HTTP METHOD in these patterns. FastAPI encoded the method in
the decorator (@app.post vs @app.get); Django routes on path only, and the
method is enforced inside the view by DRF's @api_view(["POST"]). A request with
the wrong method gets an automatic 405 Method Not Allowed.

 read more: https://docs.djangoproject.com/en/5.1/topics/http/urls/
"""

from django.urls import include, path

urlpatterns = [
    # Everything lives at the root, with no prefix, because the widget already
    # deployed on live customer sites calls https://<host>/query directly. A
    # prefix like /api/ would be tidier but would break every embedded widget in
    # the field until each customer updated their script tag. Compatibility wins.
    path("", include("navigator.urls")),
]
