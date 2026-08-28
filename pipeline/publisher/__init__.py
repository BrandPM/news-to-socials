"""Publishers: write content to the right downstream system.

* ``sanity``       — the publisher. Writes drafts to the Sanity CMS behind
                     www.iconfinance.io (ADR-018).
* ``telegram_bot`` — Telegram Bot API client. **Live, but as monitoring**: the
                     alerter (NTS_073) is its only caller.

Removed rather than kept as "Wave 2/3 groundwork" (NTS_121 §7): ``meta_graph``
(Facebook Pages + Instagram Business), the whole ``pipeline.adapter`` package
of per-channel post formatters, and ``pipeline.queue`` (publish queue, publish
windows, rate limit). None of them had a caller after the ADR-018 pivot to
Sanity — only their own tests — and code that nothing calls does not stay warm,
it stays wrong: the socials waves, if they return, return against a schema and
an API that will have moved.

The Directus writer + channel dispatcher were removed earlier, in NTS_076.
"""

from .sanity import SanityClient, SanityPostInput, SanityPublisher

__all__ = ["SanityClient", "SanityPostInput", "SanityPublisher"]
