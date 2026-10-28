"""
===============================================================================
 navigator/serializers.py — Pydantic models, rewritten as DRF serializers.
===============================================================================

This is where the port is most visible, so read it with the old classes beside
you. Every Pydantic model in api.py becomes a Serializer here, and the mapping
is mechanical once you know four rules.

    RULE 1 — required vs optional
      Pydantic: a field with no default is required.
      DRF:      every field is required unless you say `required=False`.
      So `site_id: str = "openlake.in"` becomes
         CharField(required=False, default="openlake.in")

    RULE 2 — nullable is separate from optional
      Pydantic's `str | None = None` collapses two ideas: "may be omitted" and
      "may be null". DRF splits them: `required=False` is the first,
      `allow_null=True` is the second. You often need both.

    RULE 3 — EMPTY STRING IS NOT THE SAME AS MISSING
      This is the rule that bites. Pydantic happily accepts "" for a `str`
      field. DRF's CharField REJECTS it by default with "This field may not be
      blank." The old /query endpoint accepted an empty query and let the
      pipeline deal with it, and /ask checked `body.question.strip()` itself —
      so faithfully reproducing the old behaviour means `allow_blank=True`
      almost everywhere. Forget it and the widget starts getting 400s on inputs
      that used to work.

    RULE 4 — DRF TRIMS STRINGS AND PYDANTIC DOES NOT
      CharField defaults to `trim_whitespace=True`, silently stripping leading
      and trailing whitespace. For a search query that is harmless. For the
      `html` field on /ingest it is CORRUPTION: the page hash is computed over
      the exact bytes received, so trimming would change the fingerprint and
      defeat the change-detection that makes /ingest cheap. Every field that
      carries content rather than a keyword sets `trim_whitespace=False`.

WHAT WE LOSE, STATED PLAINLY
----------------------------
Pydantic generated these shapes from ordinary type annotations, so the model was
three lines long. DRF needs an explicit field object per attribute, so this file
is noticeably more verbose than the originals. That verbosity is the honest cost
of the swap. What we gain is that these same serializers will accept
`ModelSerializer` as a base class in step 2 and generate themselves from the
database models instead — at which point the verbosity disappears again.

 read more:
   serializer fields ... https://www.django-rest-framework.org/api-guide/fields/
   validation .......... https://www.django-rest-framework.org/api-guide/serializers/#validation
"""

from rest_framework import serializers


# ============================================================================
#  /query
# ============================================================================

class QueryInSerializer(serializers.Serializer):
    """Was: class QueryIn(BaseModel)."""

    # allow_blank=True reproduces Pydantic's acceptance of "". The old endpoint
    # passed an empty query straight into answer(), which returned a refusal —
    # a 200 with found=false rather than a 400. Keeping that behaviour means the
    # widget's single `if (!data.found)` code path still covers every case.
    query = serializers.CharField(allow_blank=True)

    # A default makes the field optional. This one exists for convenience during
    # development so you can curl /query without specifying a site.
    site_id = serializers.CharField(required=False, default="openlake.in",
                                    allow_blank=True)


class QueryOutSerializer(serializers.Serializer):
    """Was: class QueryOut(BaseModel), used as FastAPI's `response_model=`.

    WHY AN OUTPUT SERIALIZER IS NOT OPTIONAL HERE
    ---------------------------------------------
    It would be tempting to skip this and just `return Response(result)`. That
    would change the API's contract in a way the deployed widget can see.

    `response_model=QueryOut` did two silent jobs on every response:
      * ADDED every missing optional key as null. answer() returns
        {"found": False, "reason": ..., "score": ...} on a refusal — no `url`
        key at all — and FastAPI turned that into a response that DID contain
        "url": null. JavaScript reading `data.url` got null rather than
        undefined. Not a difference the widget happens to care about today, but
        it is a contract change, and contract changes you did not intend are the
        ones that bite.
      * DROPPED keys not declared on the model. That `score` field was never
        reaching the client, and leaking it now would be new behaviour.

    Reproducing both is exactly what a DRF output serializer does, so the HTTP
    responses stay byte-for-byte what they were.
    """

    found = serializers.BooleanField()
    url = serializers.CharField(required=False, allow_null=True, default=None)
    selector = serializers.CharField(required=False, allow_null=True, default=None)
    heading = serializers.CharField(required=False, allow_null=True, default=None)
    explanation = serializers.CharField(required=False, allow_null=True, default=None)
    confidence = serializers.FloatField(required=False, allow_null=True, default=None)
    reason = serializers.CharField(required=False, allow_null=True, default=None)


# ============================================================================
#  /register  and  /auto-register
# ============================================================================

class RegisterInSerializer(serializers.Serializer):
    """Was: class RegisterIn(BaseModel)."""

    site_id = serializers.CharField(allow_blank=True)

    # `ListField(child=DictField())` is DRF's `list[dict]`: it validates that
    # this is a list of objects, but not what is IN them. That looseness is
    # deliberate and matches the original — the chunk fields are checked by hand
    # in the view so we can return a friendlier error message naming exactly
    # which keys are missing.
    chunks = serializers.ListField(child=serializers.DictField())


class AutoRegisterInSerializer(serializers.Serializer):
    """Was: class AutoRegisterIn(BaseModel)."""

    site_id = serializers.CharField(allow_blank=True)


# ============================================================================
#  /page-query — the Chrome extension's stateless, single-page endpoint
# ============================================================================

class PageSectionSerializer(serializers.Serializer):
    """Was: class PageSection(BaseModel)."""

    id = serializers.CharField()                      # e.g. "h5"
    tag = serializers.CharField(required=False, default="H2")
    heading = serializers.CharField(allow_blank=True, trim_whitespace=False)
    # A little text from under the heading. Content, so it is not trimmed.
    summary = serializers.CharField(required=False, default="", allow_blank=True,
                                    trim_whitespace=False)


class PageQueryInSerializer(serializers.Serializer):
    """Was: class PageQueryIn(BaseModel).

    NESTING: a Pydantic `list[PageSection]` becomes
    `PageSectionSerializer(many=True)`. DRF validates every element and hands
    the view a list of plain dicts — which is convenient, because the old view
    had to call `[s.model_dump() for s in body.sections]` to get exactly that.
    One small place where DRF is less work than Pydantic.
    """

    question = serializers.CharField(allow_blank=True)
    url = serializers.CharField(required=False, default="", allow_blank=True)
    title = serializers.CharField(required=False, default="", allow_blank=True,
                                  trim_whitespace=False)
    sections = PageSectionSerializer(many=True)


# ============================================================================
#  /site and /ask — the extension's whole-website search
# ============================================================================

class SiteLinkSerializer(serializers.Serializer):
    """Was: class SiteLink(BaseModel)."""

    url = serializers.CharField(allow_blank=True)
    text = serializers.CharField(required=False, default="", allow_blank=True,
                                 trim_whitespace=False)
    # The dropdown it sits in, e.g. "Programs".
    menu = serializers.CharField(required=False, default="", allow_blank=True,
                                 trim_whitespace=False)


class SiteInSerializer(serializers.Serializer):
    """Was: class SiteIn(BaseModel)."""

    url = serializers.CharField(allow_blank=True)       # any page of the site

    # `default=list` passes the FUNCTION, not a list literal. Passing `[]`
    # would create ONE list object shared by every request that omitted the
    # field — the classic Python mutable-default bug. Pydantic protects you from
    # this automatically; DRF does not, so it is on you to pass a callable.
    links = SiteLinkSerializer(many=True, required=False, default=list)


class AskSectionSerializer(serializers.Serializer):
    """Was: class AskSection(BaseModel)."""

    id = serializers.CharField()
    tag = serializers.CharField(required=False, default="H2")
    heading = serializers.CharField(allow_blank=True, trim_whitespace=False)
    # The text under the heading, up to ~1200 chars. Content: never trimmed.
    text = serializers.CharField(required=False, default="", allow_blank=True,
                                 trim_whitespace=False)


class AskTurnSerializer(serializers.Serializer):
    """Was: class AskTurn(BaseModel) — one earlier question/answer pair."""

    q = serializers.CharField(allow_blank=True, trim_whitespace=False)
    a = serializers.CharField(required=False, default="", allow_blank=True,
                              trim_whitespace=False)


class AskInSerializer(serializers.Serializer):
    """Was: class AskIn(BaseModel)."""

    question = serializers.CharField(allow_blank=True)
    url = serializers.CharField(allow_blank=True)
    title = serializers.CharField(required=False, default="", allow_blank=True,
                                  trim_whitespace=False)
    # The live page, read in the visitor's browser.
    sections = AskSectionSerializer(many=True, required=False, default=list)
    # Meta description + text before the first heading.
    intro = serializers.CharField(required=False, default="", allow_blank=True,
                                  trim_whitespace=False)
    # The page's text, only sent for pages with almost no headings.
    page_text = serializers.CharField(required=False, default="", allow_blank=True,
                                      trim_whitespace=False)
    # Earlier questions in this conversation.
    history = AskTurnSerializer(many=True, required=False, default=list)


# ============================================================================
#  /ingest — a page already rendered by the visitor's own browser
# ============================================================================

class IngestInSerializer(serializers.Serializer):
    """Was: class IngestIn(BaseModel)."""

    site_id = serializers.CharField(allow_blank=True)
    url = serializers.CharField(allow_blank=True)
    # Optional; the page may have no <title>.
    title = serializers.CharField(required=False, default="", allow_blank=True,
                                  allow_null=True, trim_whitespace=False)

    # THE MOST IMPORTANT trim_whitespace=False IN THIS FILE.
    # The view computes sha256 over these exact bytes to detect whether a page
    # changed since last time. If DRF stripped the leading newline that every
    # real HTML document starts with, the hash would differ from the one stored
    # by the FastAPI version — and on the first deploy EVERY page on EVERY site
    # would look changed, triggering a full re-chunk and re-embed of everything
    # at once. On a 512MB box that is not a subtle performance regression.
    html = serializers.CharField(allow_blank=True, trim_whitespace=False)
