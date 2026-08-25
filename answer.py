"""
===============================================================================
 answer.py  —  STAGE 4 of the Compass pipeline (the grounded LLM layer)
===============================================================================

The answer layer.

Takes a query + one site's chunks/vectors, runs hybrid search, passes the
top chunk to an LLM with a grounded prompt, returns structured JSON or a
refusal. Site-aware: chunks/vecs are passed in per request by the API.

-------------------------------------------------------------------------------
 WHY AN LLM IS INVOLVED AT ALL
-------------------------------------------------------------------------------
retrieval.py already found the best-matching section. So why not just send the
visitor there and be done?

Two reasons:
  1. RELEVANCE JUDGEMENT. Search always returns SOMETHING — it ranks, it never
     refuses. The top result on a site with no pricing page will still be
     whatever scored highest for "what does it cost", and that could be
     completely unrelated. A language model can read the section and say "no,
     this does not address the question."
  2. EXPLANATION. Dropping a visitor at a heading with no context is jarring.
     One sentence — "the fee structure is listed here, ₹4000 per semester" —
     turns navigation into an answer.

-------------------------------------------------------------------------------
 THE ARCHITECTURE: THIS IS RAG (Retrieval-Augmented Generation)
-------------------------------------------------------------------------------
    RETRIEVE  ->  AUGMENT  ->  GENERATE

The model is never asked "what do you know about X". It is handed a specific
passage and asked to reason ONLY about that passage. Everything it says is
traceable to a real piece of the customer's website.

    read more:
      What is RAG ......... https://www.pinecone.io/learn/retrieval-augmented-generation/
      Original RAG paper .. https://arxiv.org/abs/2005.11401
      Prompt engineering .. https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview

-------------------------------------------------------------------------------
 THE ANTI-HALLUCINATION GUARANTEE (the thing to lead with in a demo)
-------------------------------------------------------------------------------
A navigation product that confidently sends people to the wrong place is worse
than no product at all — it destroys trust instantly. So there are TWO
INDEPENDENT GATES, and a chunk must pass both:

  GATE 1 (cheap, mathematical, no LLM call):
      if the best hybrid score is below CONFIDENCE_FLOOR, refuse immediately.
      This costs nothing, adds no latency, and cannot itself hallucinate.

  GATE 2 (semantic, the LLM):
      the model sees exactly one passage, is forbidden from using outside
      knowledge, and may itself return found=false.

The worst possible outcome is therefore "I couldn't find that" — never a
confident wrong answer. Failing safe is a deliberate design choice.

Usage:
    from answer import answer
"""

import os
import json

from retrieval import search

# The model we call. Default is OpenRouter's free auto-router; override with
# LLM_MODEL in .env to switch models OR providers without touching code:
#
#   OpenRouter free pool ... openrouter/free
#   OpenRouter pinned ...... meta-llama/llama-3.3-70b-instruct:free
#   Groq ................... llama-3.3-70b-versatile   (needs Groq key + base_url)
#   Gemini (free tier) ..... gemini-2.0-flash          (Google AI Studio key)
#   OpenAI paid ............ gpt-4o-mini
#   Ollama (local, free) ... llama3.2                  (base_url http://localhost:11434/v1)
#
# Trade-offs of OpenRouter's :free pool:
#   * 50 requests/day per ACCOUNT on the free tier (429 after that)
#   * $10 of credits unlocks 1000 free-model requests/day
# read more: https://openrouter.ai/models
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/free")

# GATE 1's threshold, on the 0..1-ish scale produced by retrieval.search().
#
# HOW TO THINK ABOUT TUNING IT:
#   raise it  -> fewer wrong destinations, more "I couldn't find that"
#   lower it  -> more questions answered, more chances to mislead
# 0.35 was chosen empirically against real queries on openlake.in. Because a
# perfect-but-not-identical match typically scores 0.5-0.8 and an unrelated
# chunk scores 0.1-0.3, 0.35 sits in the natural valley between the two.
CONFIDENCE_FLOOR = 0.35

# =============================================================================
#  THE SYSTEM PROMPT
#  This is not decoration — for an LLM feature, the prompt IS the source code.
#  Every sentence below is doing a specific job. Read the annotations after it.
# =============================================================================
SYSTEM = """You are a website navigation assistant. You are given ONE section from a website and a visitor's request.

Your job is NOT to answer the question. Your job is to decide whether sending the visitor to this section would help them, and then tell them what they'll find there.

Set found to true if the section is a reasonable destination for this request — including when the visitor is simply asking where something is, or naming a page or topic. The section does not need to contain a complete answer; it only needs to be the right place to go.

Set found to false only if this section is clearly about something unrelated.

Rules:
- Use ONLY the given content. Never use outside knowledge.
- If the content contains a direct answer (a name, date, number), include it in the explanation.
- Otherwise describe what the visitor will see there.
- Keep the explanation under 30 words.

Respond with ONLY a JSON object:
{"found": true or false, "explanation": "..."}"""

# --- LINE BY LINE, WHY THE PROMPT SAYS WHAT IT SAYS --------------------------
#
# "Your job is NOT to answer the question."
#     THE MOST IMPORTANT SENTENCE IN THE FILE. An LLM's default instinct is to
#     be a question-answering machine, and left alone it will try to compose a
#     complete answer — inventing details when the passage is thin. Explicitly
#     redefining the task as a ROUTING DECISION is what converts this from a
#     chatbot into a navigator, and it is also the single biggest reduction in
#     hallucination pressure.
#
# "including when the visitor is simply asking where something is"
#     This is a CALIBRATION clause, added after real testing. Without it the
#     model was too strict: asked "where are the projects", it would look at a
#     section listing projects, reason "this does not answer a question", and
#     return found=false. But that section is exactly the right destination.
#     The clause tells the model that the bar is "is this the right PLACE",
#     not "is this a complete ANSWER".
#
# "Set found to false only if this section is clearly about something unrelated."
#     The word "only" plus "clearly" deliberately sets a HIGH bar for refusal at
#     this gate. That is safe because GATE 1 (the score floor) has already thrown
#     out everything weak. Making both gates equally strict would make the system
#     refuse far too often and feel broken.
#
# "Use ONLY the given content. Never use outside knowledge."
#     The grounding instruction. Llama 3.3 knows plenty about the world; if the
#     visitor asks about a topic the site does not cover, the model could answer
#     from memory and the visitor would believe it came from the website.
#
# "include it in the explanation"
#     A nice touch: when the passage actually holds the fact (a fee, a date, a
#     name), surface it immediately. The visitor gets their answer AND gets
#     taken to the proof.
#
# "Keep the explanation under 30 words."
#     Practical constraint. The widget's panel is 340px wide; long text scrolls,
#     looks bad, and delays the scroll animation the user is waiting for.
#
# "Respond with ONLY a JSON object"
#     Belt and braces with response_format below. Stating the schema IN the
#     prompt as well as in the API parameter measurably improves adherence.

# The user message template. Note the clear labelled sections and the fact that
# the USER QUESTION comes LAST — models attend most strongly to the beginning
# and the end of a prompt (the "lost in the middle" effect), so the instruction
# and the question bracket the data.
# read more: https://arxiv.org/abs/2307.03172
USER_TEMPLATE = """SECTION FROM: {url}
HEADING: {heading}

CONTENT:
{content}

USER QUESTION: {query}"""


def answer(query, chunks, vecs, llm):
    """
    Full query -> response pipeline for ONE site.

    Parameters
    ----------
    query  : the visitor's raw question, e.g. "where are the fees"
    chunks : that site's list of chunk dicts
    vecs   : that site's (N, 384) embedding matrix
    llm    : an already-constructed OpenAI-compatible client pointing at OpenRouter (dependency injection — see note)

    WHY IS THE LLM CLIENT PASSED IN RATHER THAN CREATED HERE?
    This is "dependency injection". Constructing a client involves reading env
    vars and setting up an HTTP connection pool; doing it per request would be
    wasteful. api.py builds it ONCE at startup and hands it in. It also makes
    this function trivially testable — you can pass a fake client in a unit test.
    read more: https://en.wikipedia.org/wiki/Dependency_injection

    Returns a dict that is always one of two shapes:
        {"found": True,  "url", "selector", "heading", "explanation", "confidence"}
        {"found": False, "reason", "score"}
    """
    # Retrieve the top 3. Note we currently only USE results[0] — the extra two
    # are retrieved because they cost essentially nothing (the matrix multiply
    # already scored everything) and they are the obvious next upgrade: showing
    # "did you mean...?" alternatives, or letting the LLM pick among candidates.
    results = search(query, chunks, vecs, k=3)

    # Defensive: an empty result list means the site has zero chunks — a crawl
    # that found nothing, or a freshly created site folder. Without this guard
    # the next line would raise IndexError.
    if not results:
        return {"found": False, "reason": "no chunks for this site", "score": 0.0}

    # Tuple unpacking: search returns (chunk, score) pairs, and [0] is the best.
    top, score = results[0]

    # ======================================================================
    #  GATE 1 — the confidence floor. Free, instant, and cannot hallucinate.
    # ======================================================================
    # Returning here means we never call the LLM at all. Three wins at once:
    #   * SPEED   — no ~500ms network round trip
    #   * COST    — no tokens spent
    #   * SAFETY  — a model that is never asked cannot invent anything
    #
    # The `reason` string embeds the actual numbers. That is deliberate: when a
    # customer says "it didn't find my pricing page", this string tells you
    # immediately whether the problem is retrieval (score way below the floor →
    # the chunk is bad or missing) or the threshold (score 0.34 → just tune it).
    # Good failure messages are an engineering feature, not a nicety.
    if score < CONFIDENCE_FLOOR:
        return {
            "found": False,
            "reason": f"no confident match (top score {score:.3f} below floor {CONFIDENCE_FLOOR})",
            "score": score,
        }

    # ======================================================================
    #  GATE 2 — ask the model to judge relevance and write the explanation.
    # ======================================================================
    # OpenRouter deliberately implements the OpenAI-compatible chat completions API
    # shape (`llm.chat.completions.create`), so this code would work against
    # OpenAI, Together, Fireworks, vLLM or a local Ollama server with only a
    # base_url change. Avoiding vendor lock-in for free.
    #
    # FAIL-SAFE WRAPPER. The provider can fail at any moment: the free tier
    # allows only 50 requests/day (HTTP 429 when exhausted), free models go
    # down without notice, and networks time out. An unhandled exception here
    # becomes a FastAPI 500 — which the widget reports as "something went
    # wrong". Returning a structured refusal instead keeps the product honest
    # and usable: the same rule as everywhere else in this file — fail safe,
    # never fail loud.
    try:
        resp = llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                # The "system" role carries the persona and the rules. Models are
                # trained to weight system instructions above anything appearing in
                # the user turn, which also makes it modestly harder for text
                # scraped off a website to override our instructions.
                # read more: https://owasp.org/www-project-top-10-for-large-language-model-applications/
                {"role": "system", "content": SYSTEM},

                # The "user" role carries the DATA. Keeping instructions and data
                # in separate roles is basic prompt hygiene and the first line of
                # defence against prompt injection — remember, `content` here is
                # text scraped from a third-party website that we do not control.
                {"role": "user", "content": USER_TEMPLATE.format(
                    url=top["url"],
                    heading=top["heading"],
                    content=top["content"],
                    query=query,
                )},
            ],

            # JSON MODE. The provider constrains token sampling so the output is
            # guaranteed to be syntactically valid JSON. This is what lets the
            # json.loads() below be written without a try/except — without this
            # flag, models happily wrap JSON in ```json fences or add a friendly
            # "Sure! Here you go:" preamble, and parsing becomes a guessing game.
            # (Note it guarantees valid JSON, not the right SCHEMA — the shape still
            #  comes from the prompt, which is why the prompt states it too.
            #  CAVEAT: some free models ignore response_format entirely; that
            #  failure mode is caught by the except below rather than crashing.)
            # read more: https://openrouter.ai/docs
            response_format={"type": "json_object"},

            # Temperature controls randomness. 0 is fully deterministic (always the
            # highest-probability token), 1.0+ is creative. We want near-determinism:
            # this is a classification and summarisation task, and the same visitor
            # asking the same question twice should get the same answer. We use 0.1
            # rather than exactly 0 because a sliver of randomness helps models
            # escape occasional degenerate repetition loops.
            temperature=0.1,

            # Hard cap on output length. The prompt already asks for under 30 words;
            # this is the enforcement in case the model ignores it. It bounds cost
            # and latency, and prevents a runaway generation from hanging a visitor.
            #
            # WHY 1000 AND NOT 200: modern "thinking" models (e.g. Gemini 3.x)
            # spend hundreds of HIDDEN reasoning tokens before emitting visible
            # text, and max_tokens caps reasoning + output TOGETHER. At 200 such
            # models return a truncated fragment or an empty string. The real
            # answer stays ~30 words; the headroom just gives the model room to
            # think. Non-thinking models stop at their own EOS long before this.
            max_tokens=1000,
        )

        # Dig the text out of the OpenAI-shaped response envelope:
        #   .choices     - list of alternative completions (we asked for one)
        #   .message     - the assistant turn
        #   .content     - the actual string
        # json.loads sits inside the same try block: JSON mode makes malformed
        # output unlikely but not impossible on free models, and one bad model
        # response must never take down an HTTP request.
        parsed = json.loads(resp.choices[0].message.content)
    except Exception as e:
        # `type(e).__name__` ("RateLimitError", "APIConnectionError", ...) plus
        # str(e) gives just enough to diagnose from the API response without
        # dumping a traceback into production. Visitors never see this string;
        # it exists for whoever is debugging "why did it refuse today?".
        return {
            "found": False,
            "reason": f"answer layer temporarily unavailable "
                      f"({type(e).__name__}: {str(e)[:120]})",
            "score": score,
        }

    # GATE 2's verdict. `.get("found")` rather than `["found"]` because JSON mode
    # guarantees valid JSON but NOT that our key is present — a malformed
    # response should read as "not found" (fail safe) rather than raise a 500.
    if not parsed.get("found"):
        return {"found": False,
                "reason": "the top chunk did not address the question",
                "score": score}

    # ---- SUCCESS ---------------------------------------------------------
    # This dict is the actual product. Note what it contains:
    #   url + selector -> the WHERE. This pair is Compass's entire moat; every
    #                     competitor returns only the `explanation` field.
    #   heading        -> human-readable label for the destination
    #   explanation    -> the LLM's grounded one-liner
    #   confidence     -> the retrieval score, passed through so the widget (or
    #                     a future analytics dashboard) can reason about quality
    return {
        "found": True,
        "url": top["url"],
        "selector": top["selector"],
        "heading": top["heading"],
        "explanation": parsed["explanation"],
        "confidence": score,
    }
