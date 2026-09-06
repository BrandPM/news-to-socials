# ADR-019 — v3: the editorial pipeline runs on documents, behind flags

**Status:** Accepted
**Date:** 2026-09-06
**Amends:** ADR-018 (Sanity remains the CMS; nothing here changes that)
**Specs:** IT_PROJ_NTS_097 through NTS_112, executed as NTS_114 sessions S1–S10

## Context

The v2 pipeline read 28 feeds, scored each item 0–10 with a model, and wrote
an article from the headline and the summary. Three things were wrong with it,
and they were wrong together:

**The article had no source.** Research (NTS_092) improved it — the model was
given facts with URLs — but "facts found by searching about a story" is not the
same thing as "the act the story is about". The first production run produced a
piece whose every number was defensible and whose central claim was not: the
pack said *18 years of experience, most recently at Credit Suisse and UBS* and
the article said *an 18-year tenure at Credit Suisse and UBS*. Right number,
right source, false claim, and every check in the system passed it.

**Nothing decided the shape of the article.** Length lived as "600–800 words"
in a prompt that had never read the material. The first run had material for
343 words and the model correctly wrote 343 — against an instruction it was
also correctly following. Whether a table belonged, how many sections there
were, what to leave out: none of it was decided anywhere.

**The cost was in the wrong place.** The daily run paid for four translations
and a cover for every article, including the ones the editorial rubric itself
classifies as rejects (NTS_105 §9). Filtering happened after the money.

## Decision

The pipeline is split into two contours with a portfolio between them.

**Contour 1 — intake (cents a day).** Feeds → dedup over three windows →
a free prefilter → one cheap editorial-guard call per item against a rubric
that lives in the database and is edited from the Content Hub → a row in
`candidates`. No generation of any kind. The funnel is reported in absolute
numbers every morning, because a rate reads identically for a dead parser and
a strict rubric.

**The portfolio.** `candidates` is a state machine with an explicit vocabulary
(NTS_098 §2). Selection out of it is a formula with logged terms, not a
judgement (NTS_100 §2): confidence, depth, freshness on a per-stage half-life,
jurisdiction tier, input kind, minus diversity penalties recomputed after every
pick. Weights live in the brand config because they were chosen by eye and are
meant to be corrected against editor decisions.

**Contour 2 — production (the money).** Runs twice a week, bounded by a weekly
budget and a per-day batch claim. For each selected candidate:

    primary document → fact pack → depth_final → plan → text → polish
        → ATTRIBUTION → data blocks → translations → internal links → Sanity

Four of those stages are the ADR:

* **The primary document comes first**, and research is demoted to filling the
  gaps it leaves. An article is not written from a retelling: a news lead with
  no document waits, retries after 48 hours, and expires with `no_document`
  rather than being written up (NTS_101 §7).
* **Depth is computed from the material**, never from the guard's read of an
  abstract. `deep` additionally requires two comparable pairs of figures,
  because `deep` promises a table and a table needs pairs.
* **Attribution runs before translation**, with one automatic fix cycle. The
  distortion above is caught by comparing subject–predicate–object against the
  source rather than by comparing digits. It advises rather than blocks: a
  check that stopped the pipeline for its own false positives would be
  switched off in a week and never switched back on.
* **The cover is drawn from the article's data** — service colour, a motif
  whose geometry is the figures, and one stamp: `CHF 5 000 000`. Deterministic,
  free, and different for two articles, which the diffusion covers were not.

**Every step is a flag, and every flag was tested in the off direction.** The
register is in the vault (NTS_127) and its executable half is
`tests/unit/test_cutover_flags_nts103.py`, which fails if a mode flag is added
without a switch-off test.

## Consequences

**What gets better.** Every figure in a published article traces to a section
of a named document read on a recorded date, without a new paid call
(`fact_packs` + `document_versions`). The expensive contour only ever runs on
material the rubric already accepted. A wrong claim is caught once, in English,
instead of four times after translation.

**What gets worse, or at least harder.** There are more states, more flags and
more tables; the failure modes are more numerous and each is narrower. The
answer is the same one this project has taken throughout: an explicit state
with a test beats an implicit one with a silent loss, and the flag register
exists so the flags do not become folklore.

**What we accept.** The document fetcher uses plain HTTP and `pypdf` rather
than Firecrawl — there is no Firecrawl credential, and regulator documents are
served as static HTML and PDF. If a source appears that needs a rendered
browser, that is one function to change (`fetch_document`), and the decision to
buy the credential can be taken then, on evidence.

**What is not decided here.** Whether the data blocks ship at all: they are
generated, tested and switched off until the Sanity schema PR (S8) is merged in
the site repository, which is not ours. The order — schema, then render, then
pipeline — is deliberate and unchanged from NTS_095.

## Alternatives considered

**Keep v2 and improve the prompts.** This was the path taken twice already
(NTS_067, NTS_092) and both times the result was better prose over the same
missing foundation. Neither could have caught the tenure distortion, because
neither had the document to compare against.

**Publish from the guard directly, with no portfolio.** Simpler, and it removes
the ability to say why one story was written and another was not — which is
the question the editor asks first and the metric NTS_113 needs.

**One flag for the whole cutover.** Rejected in NTS_103: a single switch makes
rollback all-or-nothing, and the parts fail independently. Six flags cost a
register; one flag costs a bad week.
