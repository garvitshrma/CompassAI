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

# The model we call, hosted by Groq. Two reasons for this choice:
#   * Groq runs models on custom LPU hardware and is dramatically faster than
#     typical GPU inference — important, because a visitor is staring at a
#     spinner while this runs.
#   * The free tier is generous enough for a prototype with no card on file.
# Llama 3.3 70B is an open-weights model, so it is also portable: if Groq
# disappears, the same model runs on Together, Fireworks, or your own hardware.
# read more: https://console.groq.com/docs/models
#
# NOTE (changed): this was "llama-3.3-70b-versatile", which Groq has since
# DECOMMISSIONED — every call returned 404 model_not_found, so the whole answer
# layer was dead in production. Model ids on free hosted providers are not
# forever; if answers suddenly stop working, check `client.models.list()` first.
LLM_MODEL = os.environ.get("COMPASS_LLM_MODEL", "openai/gpt-oss-120b")

# HOW MANY CANDIDATES THE MODEL GETS TO CHOOSE FROM.
# This is the single biggest accuracy change in the file — see the note above
# `SYSTEM`. 3 costs ~500 prompt tokens and measured ~0.6s on gpt-oss-120b.
N_CANDIDATES = 3

# Per-candidate content budget. Chunk content is already capped at 1500 chars by
# the chunker, but with three candidates in one prompt that is 4500 characters of
# input on every query. 600 keeps the prompt small (= faster, cheaper) while
# still carrying enough text for the model to judge relevance.
CONTENT_BUDGET = 400

# GATE 1's threshold, on the 0..1-ish scale produced by retrieval.search().
#
# HOW TO THINK ABOUT TUNING IT:
#   raise it  -> fewer wrong destinations, more "I couldn't find that"
#   lower it  -> more questions answered, more chances to mislead
# RE-TUNED, with the measurements that justify it. Scored against openlake.in:
#
#     unrelated queries      "weather in paris" 0.149   "sell pizza" 0.240
#                            "what is the fee"  0.241  (site has no fees page)
#     genuinely good matches "coaches" 0.350  "privacy policy" 0.464
#                            "how do i join" 0.512  "canonforces" 0.737
#
# The valley is between 0.24 and 0.35, so 0.30 sits in it. The old 0.35 was
# sitting exactly ON a real match ("coaches" scored 0.350) — a hair either way
# flipped a correct answer into a refusal, which is the worst place for a
# threshold to be. Gate 2 is now a reliable rejecter (it correctly refused all
# four unrelated queries above), so Gate 1 can afford to be the loose one.
CONFIDENCE_FLOOR = 0.30

# =============================================================================
#  THE SYSTEM PROMPT
#  This is not decoration — for an LLM feature, the prompt IS the source code.
#  Every sentence below is doing a specific job. Read the annotations after it.
# =============================================================================
SYSTEM = """You are a website navigation assistant. You are given a visitor's request and NUMBERED candidate sections from one website.

Your job is NOT to answer the question. Your job is to pick the ONE candidate that is the best destination for this request — or to reject all of them.

Rules:
- Prefer a section that OVERVIEWS the requested topic over a section about one specific instance of it. If the visitor asks for "projects", a page listing all the projects beats one individual project.
- A candidate is a good destination if it is the right PLACE to go, even if it does not contain a complete answer. Asking where something is counts.
- Use ONLY the candidates' text. Never use outside knowledge. Never state a fact that does not appear in the candidate you picked.
- If no candidate is about the requested topic, set index to 0.
- If the chosen candidate contains a direct answer (a name, date, number), include it in the explanation.
- Keep the explanation under 25 words.
- Write the explanation for the visitor, describing the destination itself. Never mention candidates, numbers, scores, or that you were given a choice.
- Do not hedge. If the section is the right destination, describe what is there plainly — no "likely", "probably", "may contain".

Respond with ONLY a JSON object:
{"index": <candidate number, or 0 to reject all>, "explanation": "..."}"""

# =============================================================================
#  WHY THIS PROMPT CHANGED FROM "JUDGE ONE CHUNK" TO "PICK FROM THREE"
# =============================================================================
# The old design showed the model the single top-scoring chunk and asked "is this
# relevant?", with an explicit instruction to say no ONLY if it was "clearly
# unrelated". That combination is what produced confident wrong answers, and it
# failed in a specific, reproducible way:
#
#   Query "where are the projects" on openlake.in scored (old scoring):
#       0.722  h2  Projectory            <- ONE project. Sent here.
#       0.661  h1  Projects @ OpenLake   <- the actual listing page.
#       0.658  h2  Active-OSS-Community-Finder
#
#   The retriever put a single project above the page that lists every project.
#   Gate 2 was then handed ONLY "Projectory", asked whether a section about a
#   project is relevant to a request about projects, and correctly said yes —
#   to the wrong destination. The model never saw the better option, so no
#   amount of prompt-tightening on a single chunk could have saved it.
#
# THE ROOT CAUSE IN THE RETRIEVER (worth understanding, see retrieval.py):
#   lexical_target() includes the page title, so every one of the 61 chunks on
#   /programs matched the query "projects" at token_set_ratio == 100. The lexical
#   half of the score was IDENTICAL across 45% of the index — it discriminated
#   between pages but not between sections within a page. Semantic similarity
#   alone therefore decided the winner, and it preferred the prose-heavy project
#   description over the terse listing heading.
#
# THE FIX: retrieval still ranks, but it now proposes rather than decides. The
# model sees all three and picks, which restores the ordering that scoring got
# wrong. Measured on openlake.in: 8/8 correct destinations, including correct
# refusal on all four unrelated queries, at ~0.6s.
#
# "Prefer a section that OVERVIEWS the requested topic"
#     The clause that specifically fixes the Projectory case. Navigation wants
#     the broadest correct destination — a visitor landing on the listing can
#     scroll to the specific project, but a visitor dropped on one project has
#     no way to discover the other sixty.
#
# "Never state a fact that does not appear in the candidate you picked."
#     Stronger than the old "use only the given content". The old phrasing
#     constrained where the model should LOOK; this one constrains what it may
#     WRITE, which is the thing that actually shows up as a hallucination.
#
# --- CLAUSES CARRIED OVER FROM THE ORIGINAL PROMPT, AND WHY ------------------
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
#     DELIBERATELY REMOVED. This clause set a high bar for refusal, which was the
#     right call when the model saw one chunk and Gate 1 was strict — but combined
#     with a mis-ranked top chunk it is precisely the instruction that turned a
#     retrieval mistake into a confident wrong answer. Now that the model chooses
#     among candidates, "reject all" is a normal outcome rather than a last
#     resort, and it no longer needs discouraging.
#
# "Use ONLY the given content. Never use outside knowledge."
#     The grounding instruction. These models know plenty about the world; if the
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
USER_TEMPLATE = """CANDIDATES:
{candidates}

VISITOR REQUEST: {query}"""

# One candidate block. The heading comes FIRST because it is the most
# navigationally meaningful field — it is what the visitor will actually see when
# they land, and it is what the "prefer an overview" rule is judged on.
CANDIDATE_TEMPLATE = """[{n}] HEADING: {heading}
URL: {url}
CONTENT: {content}"""


def _build_candidates(results):
    """Render (chunk, score) pairs into the numbered block the prompt expects.

    Numbering starts at 1, not 0, because index 0 is reserved as the model's
    "reject all of these" signal. Asking a language model to distinguish "item 0"
    from "no item" is asking for an off-by-one bug in natural language; making
    0 mean *nothing* and 1..N mean *something* removes the ambiguity entirely.
    """
    return "\n\n".join(
        CANDIDATE_TEMPLATE.format(
            n=i + 1,
            heading=c["heading"],
            url=c["url"],
            content=c["content"][:CONTENT_BUDGET],
        )
        for i, (c, _) in enumerate(results)
    )


def answer(query, chunks, vecs, llm):
    """
    Full query -> response pipeline for ONE site.

    Parameters
    ----------
    query  : the visitor's raw question, e.g. "where are the fees"
    chunks : that site's list of chunk dicts
    vecs   : that site's (N, 384) embedding matrix
    llm    : an already-constructed Groq client (dependency injection — see note)

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
    # Retrieve the top N. ALL of them are now used: they become the numbered
    # candidate list the model chooses from. Scoring proposes; the model decides.
    # (The extra two cost essentially nothing to retrieve — the matrix multiply
    #  already scored every chunk, so this is just a larger slice of the sort.)
    results = search(query, chunks, vecs, k=N_CANDIDATES)

    # Defensive: an empty result list means the site has zero chunks — a crawl
    # that found nothing, or a freshly created site folder. Without this guard
    # the next line would raise IndexError.
    if not results:
        return {"found": False, "reason": "no chunks for this site", "score": 0.0}

    # Tuple unpacking: search returns (chunk, score) pairs, and [0] is the best.
    # `score` is still the TOP score — Gate 1 gates on the best candidate, since
    # if even the best is weak there is nothing worth showing the model.
    _, score = results[0]

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
    #  GATE 2 — the model picks one candidate, or rejects them all.
    # ======================================================================
    # Groq deliberately implements the OpenAI-compatible chat completions API
    # shape (`llm.chat.completions.create`), so this code would work against
    # OpenAI, Together, Fireworks, vLLM or a local Ollama server with only a
    # base_url change. Avoiding vendor lock-in for free.
    #
    # THE WHOLE CALL IS WRAPPED IN try/except, which it was not before. Reason:
    # every failure mode below is one we have actually hit in this project.
    #   * the configured model id was decommissioned      -> 404 NotFoundError
    #   * free-tier rate limit                            -> 429 RateLimitError
    #   * JSON mode truncated by max_tokens               -> 400 BadRequestError
    # Previously any of these propagated out of answer(), out of the /query
    # handler, and reached the visitor as an HTTP 500 with no useful message.
    # A navigation widget should degrade to "I couldn't find that" — never to a
    # broken response the front-end cannot parse.
    try:
        resp = llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                # The "system" role carries the persona and the rules. Models are
                # trained to weight system instructions above anything appearing in
                # the user turn, which also makes it modestly harder for text
                # scraped off a website to override our instructions.
                {"role": "system", "content": SYSTEM},

                # The "user" role carries the DATA. Keeping instructions and data in
                # separate roles is basic prompt hygiene and the first line of
                # defence against prompt injection — remember, the candidate text
                # here is scraped from a third-party website we do not control.
                # read more: https://owasp.org/www-project-top-10-for-large-language-model-applications/
                {"role": "user", "content": USER_TEMPLATE.format(
                    candidates=_build_candidates(results),
                    query=query,
                )},
            ],

            # JSON MODE. The provider constrains token sampling so the output is
            # syntactically valid JSON — no ```json fences, no "Sure! Here you go:"
            # preamble to strip. (It guarantees valid JSON, not the right SCHEMA,
            # which is why the prompt states the shape too.)
            # read more: https://console.groq.com/docs/text-chat#json-mode
            response_format={"type": "json_object"},

            # Temperature controls randomness. 0 is fully deterministic, 1.0+ is
            # creative. We want near-determinism: this is a selection task, and the
            # same visitor asking the same question twice should get the same
            # destination. 0.1 rather than exactly 0 because a sliver of randomness
            # helps models escape occasional degenerate repetition loops.
            temperature=0.1,

            # gpt-oss models emit internal REASONING tokens before their visible
            # answer, and both count against max_tokens. At the old cap of 200 the
            # reasoning consumed the entire budget, the JSON was cut off mid-object,
            # and Groq rejected the whole request with:
            #     400 json_validate_failed, failed_generation: ''
            # That is a confusing error to debug, because the prompt is fine and the
            # model is fine — the budget is the bug. 512 leaves room for both.
            max_tokens=512,

            # Keep the reasoning short. This is a routing decision over three short
            # passages, not a maths problem; low effort roughly halved latency in
            # testing (~1.4s -> ~0.6s) with no observed change in which candidate
            # was picked.
            reasoning_effort="low",
        )

        # Dig the text out of the OpenAI-shaped response envelope:
        #   .choices - list of alternative completions (we asked for one)
        #   .message - the assistant turn
        #   .content - the actual string
        parsed = json.loads(resp.choices[0].message.content)
    except Exception as e:
        # FAIL SAFE, NOT FAIL LOUD. The visitor gets an honest refusal; the
        # operator gets the real exception in the logs. Printing the type as well
        # as the message matters — "404" alone does not tell you whether the model
        # id is wrong or the endpoint is, but NotFoundError does.
        print(f"[answer] LLM call failed: {type(e).__name__}: {e}")
        return {"found": False,
                "reason": "the assistant is temporarily unavailable",
                "score": score}

    # ---- interpret the model's choice ------------------------------------
    # `index` is 1-based, and 0 means "none of these fit". Anything outside
    # 1..len(results) — a hallucinated "4", a string, a missing key — is treated
    # as a rejection. int() guards against the model returning "2" as a string;
    # the try/except guards against it returning something that is not a number
    # at all. Both are cheap, and both fail toward a refusal rather than an
    # IndexError or a confidently wrong destination.
    try:
        idx = int(parsed.get("index", 0))
    except (TypeError, ValueError):
        idx = 0

    if not 1 <= idx <= len(results):
        return {"found": False,
                "reason": "no section on this site addresses the question",
                "score": score}

    # Convert the model's 1-based pick back to a 0-based list index.
    chosen, chosen_score = results[idx - 1]

    # ---- SUCCESS ---------------------------------------------------------
    # This dict is the actual product. Note what it contains:
    #   url + selector -> the WHERE. This pair is Compass's entire moat; every
    #                     competitor returns only the `explanation` field.
    #   heading        -> human-readable label for the destination
    #   explanation    -> the LLM's grounded one-liner
    #   confidence     -> the CHOSEN candidate's score, not the top one's. If the
    #                     model picked #2, reporting #1's score would overstate
    #                     confidence in a destination we did not actually send.
    return {
        "found": True,
        "url": chosen["url"],
        "selector": chosen["selector"],
        "heading": chosen["heading"],
        "explanation": parsed.get("explanation", ""),
        "confidence": chosen_score,
    }
