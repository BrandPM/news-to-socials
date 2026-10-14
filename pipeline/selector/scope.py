"""Is this candidate about private wealth at all? (NTS_133 P5 Ф2)

The guard accepts by service category, and one of Icon's five categories is
``ma`` — mid-market deals, because Icon advises owners selling a business. The
guard reads that generously: the 2026-10 production run took XXXLutz buying
Porta (furniture retail) as an ``ma`` story, next to Sika/Akkim (chemicals),
Bodycote (engineering) and Itochu/Dentsu Soken. None of them is about a
private owner, a family or the people who manage their money. The rubric is
not the place to fix it: it is the accept gate, its recall is measured, and a
narrower M&A clause would cost the deals Icon does want — a founder's exit,
a wealth manager bought by a bank.

So scope is a ranking term with a floor, computed from what the row already
holds, for free:

* ``wealth``, ``family``, ``structuring``, ``special`` — in scope by
  construction (the guard placed them in a private-wealth service);
* ``ma`` — in scope only with a private-wealth signal in the title or summary:
  a wealth manager, private bank, family office, founder, family-owned
  business, succession;
* anything else, or ``ma`` with a retail/industrial marker and no signal —
  out of scope.

An out-of-scope candidate scores 0 on the term and is never auto-selected; a
manager can still promote it, and the promotion is honoured (NTS_100 §1).
"""

from __future__ import annotations

import re

# The four categories that are private wealth by definition (brand_taxonomy).
PRIVATE_WEALTH_CATEGORIES = frozenset({"wealth", "family", "structuring", "special"})

# A deal is Icon's when one of its parties is wealth or a private owner. EN /
# DE / FR / ES / IT / RU — the languages the feeds actually carry.
_WEALTH_SIGNAL = re.compile(
    r"wealth|private bank|private client|family office|family[- ]owned|"
    r"\bfamily business|\bfounders?\b|owner[- ]managed|\bheirs?\b|succession|"
    r"\bRIAs?\b|registered investment advis|independent advis|"
    r"asset manag|investment manag|fund manag|trust compan|fiduciar|"
    r"\bUHNW|\bHNW|high[- ]net[- ]worth|multi[- ]family|"
    r"vermögensverwalt|privatbank|familienunternehm|nachfolge|"
    r"gestion de (?:patrimoine|fortune)|banque privée|"
    r"banca privada|banca privata|gestione patrimon|patrimonio|"
    r"управлени[ея] капитал|частн[а-я]+ банк|семейн[а-я]+ офис",  # noqa: RUF001
    re.IGNORECASE,
)

# Markers of the corporate flow the directive names: industrial M&A, retail,
# general corporate news. Only consulted when there is no wealth signal — a
# family-owned retailer selling to a fund is still a family's exit.
_OFF_SCOPE_MARKER = re.compile(
    r"retail|furniture|möbel|supermarket|grocer|apparel|fashion|"
    r"industrial|chemical|manufactur|engineering|automotive|aerospace|"
    r"semiconductor|pharma|biotech|oil and gas|mining|steel|logistics|"
    r"telecom|gaming|casino|restaurant|hotel chain|real estate developer",
    re.IGNORECASE,
)


def scope_of(
    service_category: str | None,
    *,
    title: str | None = None,
    summary: str | None = None,
) -> float:
    """1.0 in scope, 0.0 out — the ``scope`` term of the rank formula."""
    category = (service_category or "").strip().lower()
    if category in PRIVATE_WEALTH_CATEGORIES:
        return 1.0
    if category != "ma":
        return 0.0
    text = f"{title or ''}\n{summary or ''}"
    if _WEALTH_SIGNAL.search(text):
        return 1.0
    # ``ma`` without a wealth signal is out whether or not it names an
    # industry: «Veritas Capital outbids CVC in £1.65bn Bodycote deal» names
    # none, and it is the same corporate flow as the furniture chain. The
    # marker is logged by the caller so the reason is readable, not because
    # it changes the answer.
    return 0.0


def off_scope_marker(title: str | None, summary: str | None = None) -> str | None:
    """The first retail/industrial word in the text, for the ranking log."""
    match = _OFF_SCOPE_MARKER.search(f"{title or ''}\n{summary or ''}")
    return match.group(0).lower() if match else None
