# content/

Hand-written knowledge for the bot. **One file per site, named `<site_id>.txt`.**

    content/openlake-facts.txt   ->  python tools/ingest_text.py content/openlake-facts.txt --site openlake-facts

## Why this folder is in git and `sites/` is not

These two look similar and are opposites:

| | `sites/` | `content/` |
|---|---|---|
| Produced by | a crawl | a person |
| Recoverable | yes, re-crawl | **no** |
| Committed | no | **yes** |

`sites/` holds chunks and embeddings — derived artifacts, megabytes, rebuilt on
demand. Correctly gitignored. `content/` holds the only copy of things nobody
can regenerate: the fee that is only quoted over email, who to contact about
admissions, the fact that the office shuts on Fridays. Losing it means asking a
human to remember it again.

Small, text, irreplaceable, and worth seeing the diff of — that is exactly what
version control is for.

## Format

    title: OpenLake                  optional, once, at the top

    # Fees and membership            a heading starts a chunk (level h1)
    OpenLake is free to join.        body text until the next heading

    ## Who can join                  ## is h2, ### is h3
    url: https://openlake.in/join    optional, FIRST line under the heading
    Any IIT Bhilai student can join.

- A heading with no body under it is skipped. An empty chunk can still win a
  similarity match and then have nothing to say — a confident empty answer,
  which is worse than no answer.
- `url:` gives page-level navigation ("Take me there →") but no scroll target,
  because a typed fact has no element on the page to scroll to.
- Text above the first heading is ignored, so notes at the top are fine.

## Re-running

`/register` REPLACES a site's whole index. So editing this file and re-running
the script is the full update cycle — there is nothing to clean up first. But
for the same reason, never point it at a `site_id` that holds crawled chunks:
they would be deleted, not merged. Merging the two belongs in the database work.
