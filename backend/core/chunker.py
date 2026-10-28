"""
===============================================================================
 chunker.py  —  STAGE 2 of the Compass pipeline
===============================================================================

WHAT THIS FILE DOES
-------------------
Reads the raw HTML that crawler.py saved to disk and cuts each page into
"chunks". A chunk is one heading plus the text that lives underneath it — and,
crucially, a CSS SELECTOR that points at that heading inside the live page.

WHY CHUNK AT ALL? (the core idea behind every RAG system)
---------------------------------------------------------
We want to search the site semantically, which means turning text into vectors.
But you cannot usefully embed a whole 5000-word page into one vector: the result
is a blurry average of every topic on the page and matches nothing well. This is
sometimes called "semantic dilution".

So we split pages into topic-sized pieces. Headings are a near-perfect split
point because the site's own author already used them to mark topic boundaries.
We are borrowing the human structure that is already in the document rather than
inventing our own.

    read more (chunking strategies):
      https://www.pinecone.io/learn/chunking-strategies/

WHY THE CSS SELECTOR IS THE WHOLE PRODUCT
-----------------------------------------
Every competitor (Chatbase, SiteGPT, CustomGPT, DocsBot) stores
    (text -> answer)
and prints the answer in a chat box.

Compass stores
    (text -> LOCATION)
where a location is (url + css_selector). That lets the browser widget run
`document.querySelector(selector)`, scroll to the real element, and outline it.
The visitor SEES the answer in its original context instead of having to trust a
paragraph of generated text.

That single extra field, produced by css_path() below, is the entire
differentiator of this product. If you understand one function in this codebase,
make it css_path.

READ MORE
---------
  CSS selectors ......... https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_selectors
  :nth-of-type() ........ https://developer.mozilla.org/en-US/docs/Web/CSS/:nth-of-type
  querySelector ......... https://developer.mozilla.org/en-US/docs/Web/API/Document/querySelector
  Document outline / h1-h6  https://developer.mozilla.org/en-US/docs/Web/HTML/Element/Heading_Elements

Usage:
    python chunker.py

Input:   pages/*.html  +  pages/index.json   (from crawler.py)
Output:  chunks.json
"""

import json
from pathlib import Path

from bs4 import BeautifulSoup

PAGES = Path("pages")
OUT = Path("chunks.json")

# Which heading levels we treat as chunk boundaries.
# We stop at h3 deliberately. h4/h5/h6 are usually sub-labels inside a section
# ("Prerequisites", "Note") rather than navigable destinations, and including
# them would flood the index with tiny fragments that compete with the real
# sections during search.
HEADINGS = ["h1", "h2", "h3"]

# "Chrome" is the standard UI word for the frame around the content — the parts
# of a page that repeat everywhere and are not the page's actual subject.
# read more: https://www.nngroup.com/articles/browser-and-gui-chrome/
CHROME = ["nav", "header", "footer", "aside"]   # not real content

# Below this many characters a chunk is treated as too thin to be worth
# indexing. It would add noise to search without ever being a useful answer.
MIN_CHARS = 20                                   # drop near-empty chunks

# Above this we truncate. Two reasons:
#  1. embedding models have a token limit and silently cut off anything beyond
#     it, so long text is wasted work
#  2. a huge chunk means a blurry vector (semantic dilution again)
MAX_CHARS = 1500                                 # truncate runaway sections


def in_chrome(tag):
    """True if this heading lives inside a nav/header/footer.

    WHY THIS FILTER IS ESSENTIAL
    ----------------------------
    Site navigation appears on EVERY page. If you index it, then for a 10-page
    site the phrase "Contact Us" from the navbar appears 10 times in your index.
    Now every query drags in near-duplicate navbar chunks, they crowd out the
    real content in the top-k results, and the widget cheerfully scrolls the
    visitor to the navigation bar they were already looking at.

    Removing chrome is not a nicety — without it, retrieval quality collapses.

    HOW IT WORKS
    ------------
    BeautifulSoup gives every tag a `.parents` generator that yields its parent,
    then grandparent, and so on up to the document root. We walk that chain
    looking for two different signals.
    read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#parents
    """
    for parent in tag.parents:
        # SIGNAL 1: a semantic HTML5 landmark element. This is the clean case —
        # the site author used <nav>/<footer> correctly and told us outright
        # that this region is not content.
        # read more: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/nav
        if parent.name in CHROME:
            return True

        # SIGNAL 2: a class-name heuristic, for the very common case where the
        # site used <div class="navbar"> instead of <nav>. Bootstrap, Tailwind
        # templates and most WordPress themes do exactly this.
        #
        # `parent.get("class", [])` returns a LIST, because an element can carry
        # many classes (class="navbar navbar-dark fixed-top"). BeautifulSoup
        # splits them for us. The `[]` default matters: without it, an element
        # with no class attribute returns None and " ".join(None) would raise.
        # We join them back into one string so a single substring test covers
        # every class at once.
        classes = " ".join(parent.get("class", [])).lower()

        # `any(...)` with a generator is the idiomatic "is at least one of these
        # true" — it also short-circuits on the first match.
        # "nav-" (with the hyphen) is deliberately narrower than "nav", because
        # a bare "nav" substring would also match innocent words like
        # "navigation-guide" or "navy".
        if any(w in classes for w in ("navbar", "nav-", "footer", "menu")):
            return True
    return False


def css_path(tag):
    """
    Build a unique CSS selector for a tag with no id.

    Walks up the tree collecting tag + :nth-of-type(n) at each level,
    which produces something the browser can resolve exactly.
    e.g. "main > section:nth-of-type(3) > h2:nth-of-type(1)"

    ===========================================================================
    THIS IS THE MOST IMPORTANT FUNCTION IN COMPASS. Read it slowly.
    ===========================================================================

    THE GOAL
    --------
    We are on the SERVER, holding a parsed copy of the HTML. We need to produce
    a string that, months later, in a totally different process (the visitor's
    BROWSER), can be handed to `document.querySelector(...)` and will find the
    exact same element. We are essentially serialising a pointer into a document
    so it can survive the trip across the network.

    THE STRATEGY, IN ORDER OF PREFERENCE
    ------------------------------------
    1. If the element has an id, use "#that-id" and stop immediately.
       Ids are unique by definition in HTML, so this is the shortest and by far
       the most robust selector possible. It also survives a site redesign that
       moves the element somewhere else on the page.

    2. Otherwise, describe the element by its POSITION in the tree, walking
       upward and recording "which sibling of my kind am I?" at each level.

    3. While walking up, if we ever reach an ancestor that HAS an id, anchor
       there and stop. "#main > h2:nth-of-type(2)" is far more robust than
       "html > body > div > div > main > h2:nth-of-type(2)", because it does not
       care about wrapper divs being added above #main later.

    WHY :nth-of-type AND NOT :nth-child?
    ------------------------------------
    :nth-child(3) means "the 3rd child, whatever its tag".
    :nth-of-type(3) means "the 3rd <h2> among my siblings".
    The second is much more stable: if someone later inserts a <p> before our
    heading, :nth-child would now point at the wrong element, while
    :nth-of-type still counts only h2s and is unaffected.
    read more: https://developer.mozilla.org/en-US/docs/Web/CSS/:nth-of-type

    THE KNOWN WEAKNESS (be honest about this if asked)
    --------------------------------------------------
    Structural selectors are inherently brittle: if the site is redesigned, the
    tree changes and stale selectors stop resolving. Compass handles this
    gracefully rather than perfectly — the widget's highlight() wraps
    querySelector in a try/catch and falls back to "here's the link" if the
    element cannot be found. The real fix is re-indexing, which is exactly what
    the /ingest endpoint in api.py does automatically as visitors browse.
    """
    # --- BEST CASE: the element already has a unique id --------------------
    # `tag.get("id")` returns None when the attribute is absent, and None is
    # falsy, so this reads naturally as "if it has an id".
    if tag.get("id"):
        return f"#{tag['id']}"

    parts = []          # collected selector fragments, built BOTTOM-UP
    node = tag          # the cursor we move up the tree

    # "[document]" is BeautifulSoup's synthetic name for the root object that
    # wraps the whole parsed document. Reaching it means we ran out of tree.
    while node is not None and node.name != "[document]":
        parent = node.parent

        # Guard: if we are already at the top, just record our tag name and
        # stop. Without this the sibling logic below would crash on None.
        if parent is None or parent.name == "[document]":
            parts.append(node.name)
            break

        # --- position among siblings of the same tag name -------------------
        # `recursive=False` is the critical flag here. By default BeautifulSoup
        # searches the ENTIRE subtree; with recursive=False it looks only at
        # DIRECT children. We need direct children only, because CSS's
        # :nth-of-type also counts only among immediate siblings. Get this wrong
        # and every selector you generate is subtly, silently incorrect.
        # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#recursive
        siblings = [s for s in parent.find_all(node.name, recursive=False)]

        if len(siblings) > 1:
            # There are several tags of this type side by side, so we must
            # disambiguate by index. `.index(node)` finds our position;
            # `+ 1` converts Python's 0-based counting to CSS's 1-based
            # counting — CSS says :nth-of-type(1) for the first element.
            idx = siblings.index(node) + 1
            parts.append(f"{node.name}:nth-of-type({idx})")
        else:
            # We are the only one of our kind at this level, so the bare tag
            # name is already unambiguous. Keeping the selector short makes it
            # more readable and slightly more robust.
            parts.append(node.name)

        # --- early exit on an id'd ancestor --------------------------------
        # An id is unique across the whole document, so once we reach one there
        # is no need to keep describing the path to the root. Everything above
        # this point is irrelevant — and NOT describing it means later changes
        # up there cannot break our selector.
        if parent.get("id"):
            parts.append(f"#{parent['id']}")
            break

        node = parent          # step up one level and repeat

    # We collected fragments from the element upward (child -> parent), but CSS
    # is written from the outside in (parent -> child), so we reverse.
    # " > " is the CSS "direct child" combinator, which is stricter and faster
    # to evaluate than a plain space (the "any descendant" combinator).
    # read more: https://developer.mozilla.org/en-US/docs/Web/CSS/Child_combinator
    return " > ".join(reversed(parts))


def level(tag):
    """Extract the numeric rank of a heading: "h2" -> 2.

    tag.name is a string like "h1"/"h2"/"h3", so tag.name[1] is the character
    after the "h", and int() turns that character into a number we can compare
    with < and >. This tiny helper is what lets text_until_next_heading() reason
    about heading HIERARCHY rather than just heading identity.
    """
    return int(tag.name[1])


def text_until_next_heading(heading):
    """
    Collect the body text belonging to one heading.

    THE RULE
    --------
    A section runs from its heading until the next heading of the SAME OR
    HIGHER rank (remember: higher rank = smaller number, h1 outranks h2).

    So for this document:

        <h2>Projects</h2>          <-- we are here
          <p>We build things.</p>       collected
          <h3>Marketplace</h3>          collected (h3 is LOWER rank, it is ours)
          <p>Buy and sell.</p>          collected
        <h2>Team</h2>              <-- STOP, same rank, a new sibling section

    ...the "Projects" chunk correctly contains its own child subsection, and
    stops cleanly at the next peer. This mirrors how a human reads a document
    outline, and it is why the comparison is `<=` rather than `==`.
    """
    out = []
    my_level = level(heading)

    # WHY THIS `own` SET EXISTS — a subtle bug guard.
    # `find_all_next()` walks forward through the entire document in document
    # order, and that includes the heading's OWN children. If the heading is
    # <h2><a href="#x">Projects</a></h2>, then that inner <a> is "next" in
    # document order and we would collect the word "Projects" AGAIN into the
    # body — so the chunk text would read "Projects. Projects ...".
    #
    # `id()` is Python's built-in returning an object's unique memory address.
    # We use it because comparing BeautifulSoup tags with == compares their
    # HTML content, so two identical <p>Hi</p> tags would compare equal even
    # though they are different elements. `id()` gives us true object identity.
    # read more: https://docs.python.org/3/library/functions.html#id
    own = set(id(d) for d in heading.descendants)   # skip heading's own children

    # find_all_next() yields every element after this one in document order,
    # flattened — it does NOT respect nesting, which is exactly what we want
    # here since sections are defined by heading order, not by DOM containment.
    # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#find-all-next
    for el in heading.find_all_next():
        if id(el) in own:
            continue                    # our own child, already in the heading

        # THE STOP CONDITION: a heading at the same or higher rank starts a new
        # section, so this one is over.
        if el.name in HEADINGS and level(el) <= my_level:
            break

        # Only harvest text from elements that typically CONTAIN prose. We skip
        # structural wrappers because their text would be collected again from
        # their children.
        if el.name in ("p", "li", "td", "span", "a", "div"):
            # `find(string=True, recursive=False)` takes only this element's own
            # DIRECT text node, not its descendants'. This is the second half of
            # the duplication guard: for <div><p>Hello</p></div>, without
            # recursive=False the div would yield "Hello" and then the <p> would
            # yield "Hello" again on the next loop iteration.
            # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#the-string-argument
            t = el.find(string=True, recursive=False)

            # Two checks in one: `t` may be None (no direct text at all), and
            # `t.strip()` may be empty (the element held only whitespace or a
            # newline, which is extremely common in formatted HTML).
            if t and t.strip():
                out.append(t.strip())

        # Length guard checked every iteration so a section with no following
        # heading (e.g. the last section of a very long page) cannot make us
        # walk thousands of elements for text we are about to throw away.
        if sum(len(x) for x in out) > MAX_CHARS:
            break

    # Join the fragments with spaces, then hard-truncate. The slice is a second
    # safety net: the loop above breaks only AFTER exceeding MAX_CHARS, so the
    # accumulated text can overshoot by the length of one final fragment.
    return " ".join(out)[:MAX_CHARS]

def chunk_page(html, url, title):
    """Turn one page's HTML into a list of chunk dictionaries."""
    soup = BeautifulSoup(html, "html.parser")

    # strip things that never contain answerable content
    # -------------------------------------------------------------------
    # `soup([...])` is shorthand for `soup.find_all([...])`.
    # `.decompose()` destroys the tag AND its contents, permanently, in place.
    # (The alternative, .extract(), removes but returns the tag for reuse —
    #  we do not need it back, and decompose frees the memory.)
    #
    # This matters more than it looks: a modern page can carry hundreds of
    # kilobytes of inline JavaScript and CSS. Without stripping it, minified JS
    # keywords end up inside your chunk text and inside your embeddings, which
    # poisons search results with gibberish.
    # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#decompose
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    chunks = []
    # find_all with a LIST of names matches any of them, and returns the results
    # in DOCUMENT ORDER — which is what makes the "until the next heading" logic
    # in text_until_next_heading valid.
    for h in soup.find_all(HEADINGS):
        if in_chrome(h):
            continue                     # navbar/footer heading, not content

        # get_text(" ", strip=True) flattens nested markup into plain text.
        # The " " separator matters: for <h2>Open<span>Lake</span></h2> the
        # default would produce the mangled "OpenLake"... actually it would
        # produce "OpenLake" with no space at all, so we pass " " to keep words
        # apart. strip=True trims whitespace around each piece.
        # read more: https://www.crummy.com/software/BeautifulSoup/bs4/doc/#get-text
        heading_text = h.get_text(" ", strip=True)
        if not heading_text:
            continue                     # an empty <h2></h2> used for spacing

        body = text_until_next_heading(h)

        # The stored content deliberately REPEATS the heading at the front,
        # followed by ". " so it reads as a sentence. Two benefits:
        #   1. the embedding of this chunk gets extra weight on the heading, so
        #      short label-like headings are not drowned out by body text
        #   2. when this text is later shown to the LLM in answer.py, the model
        #      sees the topic before the detail, which improves grounding
        content = f"{heading_text}. {body}".strip()

        if len(content) < MIN_CHARS:
            # keep bare headings only if they look navigable (h1/h2)
            # -----------------------------------------------------------
            # A nearly-empty h1 or h2 is still a legitimate DESTINATION — think
            # of an h1 "Contact" above a form with no prose, or an h2 "Gallery"
            # above a grid of images. A visitor asking "where's the gallery"
            # should still be taken there. So we keep those.
            #
            # A nearly-empty h3 is different: it is a sub-label deep inside a
            # section, it carries no standalone meaning, and its parent h2 is
            # already indexed and is the better destination anyway.
            if h.name == "h3":
                continue

        # The chunk record. Every downstream stage depends on this exact shape,
        # so it is worth memorising:
        chunks.append({
            "url": url,               # where the widget must navigate to
            "page_title": title,      # extra context for embedding + display
            "heading": heading_text,  # what we show the user; weighted in search
            "level": h.name,          # "h1"/"h2"/"h3" — used for the rank bonus
                                      # in retrieval.py's scoring formula
            "selector": css_path(h),  # THE differentiator: where to scroll to
            "content": content,       # the text that actually gets embedded
        })
    return chunks


def main():
    """Read every crawled page, chunk it, and write one combined chunks.json."""
    # index.json is our manifest from crawler.py. We iterate IT rather than
    # globbing *.html, because only the manifest knows the original URL for each
    # saved file — and the URL is what the widget needs to navigate.
    index = json.loads((PAGES / "index.json").read_text(encoding="utf-8"))

    all_chunks = []
    for page in index:
        html = (PAGES / page["file"]).read_text(encoding="utf-8")
        cs = chunk_page(html, page["url"], page["title"])
        # `.extend` appends every element of `cs` individually. Using `.append`
        # here would instead nest a list inside a list — a classic slip.
        all_chunks.extend(cs)
        print(f"{len(cs):3d} chunks  <-  {page['url']}")

    # ---- assign global sequential ids ------------------------------------
    # This looks like bookkeeping but it encodes an important INVARIANT:
    #
    #     chunk["id"] == its row index in embeddings.npy
    #
    # retrieval.py embeds the chunks in exactly this order and stores the result
    # as one big NumPy matrix. Because position is preserved, searching is a
    # single matrix multiply (`vecs @ q`) and the winning row index is directly
    # the winning chunk — no lookup table, no database, no join.
    # If you ever reorder or filter chunks without re-embedding, you silently
    # break this correspondence and search starts returning the wrong text.
    # read more (enumerate): https://docs.python.org/3/library/functions.html#enumerate
    for i, c in enumerate(all_chunks):
        c["id"] = i

    # ensure_ascii=False keeps real Unicode characters (é, —, हिंदी) readable in
    # the file instead of escaping them to \uXXXX. Purely for human debugging;
    # JSON is valid either way.
    # read more: https://docs.python.org/3/library/json.html#json.dump
    OUT.write_text(json.dumps(all_chunks, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nTotal {len(all_chunks)} chunks -> {OUT}")

    # Print a few samples. This is not decoration — eyeballing the selector and
    # the content of real chunks is the fastest way to catch a chunking bug
    # before it silently degrades every downstream search result.
    print("\n--- sample ---")
    for c in all_chunks[:3]:
        print(f"\n[{c['id']}] {c['heading']}  ({c['level']})")
        print(f"    url:      {c['url']}")
        print(f"    selector: {c['selector']}")
        print(f"    content:  {c['content'][:120]}...")


if __name__ == "__main__":
    main()
