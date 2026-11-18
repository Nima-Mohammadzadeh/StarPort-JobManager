"""
customers.py — one customer, however the name is typed
=======================================================
Customers with their own data sheet (GO2, CDO, Venture Research) must be recognised no matter
how someone writes them: "go 2", "Go2Partners LLC", "G0 2", "venture", "Ventrue Research, Inc."
all mean one customer, one folder and one data sheet.

How a typed name is matched, in order (the first that fits wins):
  1. name         the customer's own name, ignoring case, spaces and punctuation
  2. alias        a known other way of writing it (Go2Partners, Go Two, Venture …)
  3. starts with  the full company name followed by more words ("Venture Research Austin")
  4. spelling     one or two letters off a longer alias ("Ventrue Research", "Go2Partenrs")

Guards, so another customer is never pulled in:
  • company endings (Inc, LLC, Co …) and punctuation are ignored, 0 is read as O
  • short names (GO2, CDO) must match exactly: "G2", "GO23", "go3" are other customers
  • a spelling match may not change any digit ("Go Partners" is not GO2)
  • a name that is exactly another known customer (lists.CUSTOMERS, or `known`) stays that customer,
    and a spelling match is dropped if another known customer is just as close
"""
import re
from collections import namedtuple

Match = namedtuple("Match", "customer how")

# Canonical name → share folder and the other ways it is written. `full` are the complete company
# names that may be followed by more words. Keys are compared after _key() (lowercase, letters and
# digits only, "two" → 2, 0 → o, company endings dropped).
SHEET_CUSTOMERS = {
    "GO2": {"folder": "Go2Partners",
            "aliases": ["go2", "go2partners", "go2partner", "go2p"],
            "full": ["go2partners", "go2partner"]},
    "CDO": {"folder": None, "aliases": ["cdo"], "full": []},
    "Venture Research": {"folder": "Venture Research",
                         "aliases": ["venture", "ventureresearch"],
                         "full": ["ventureresearch"]},
}

_ENDINGS = {"inc", "incorporated", "llc", "ltd", "limited", "co", "corp", "corporation", "company", "the"}
_NUMBER_WORDS = {"two": "2"}
TYPO_MIN_LEN = 6          # aliases shorter than this must match exactly


def _tokens(name) -> list:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower().replace("&", " and "))
    words = [_NUMBER_WORDS.get(w, w) for w in words]
    return [w for i, w in enumerate(words) if i == 0 or w not in _ENDINGS]


def _fold(s: str) -> str:
    return s.replace("0", "o")


def _key(name) -> str:
    return _fold("".join(_tokens(name)))


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _distance(a: str, b: str) -> int:
    """Edits to turn a into b: insert, delete, change, or swap two neighbours (each counts 1)."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _allowed_typos(alias: str) -> int:
    if len(alias) < TYPO_MIN_LEN:
        return 0
    return 1 if len(alias) <= 10 else 2


def _known_others(known) -> dict:
    """Key → name for every known customer that is not one of ours."""
    if known is None:
        try:
            from lists import CUSTOMERS as known
        except ImportError:                   # pragma: no cover
            known = []
    ours = {_key(a) for c, cfg in SHEET_CUSTOMERS.items() for a in [c, cfg["folder"] or c, *cfg["aliases"]]}
    out = {}
    for n in known:
        k = _key(n)
        if k and k not in ours:
            out[k] = n
    return out


def match(name, known=None):
    """The data-sheet customer this name means, as Match(customer, how), or None."""
    toks = _tokens(name)
    key = _fold("".join(toks))
    if not key:
        return None
    for cust, cfg in SHEET_CUSTOMERS.items():
        if key == _key(cust):
            return Match(cust, "name")
    for cust, cfg in SHEET_CUSTOMERS.items():
        if key in {_key(a) for a in [cfg["folder"] or cust, *cfg["aliases"]]}:
            return Match(cust, "alias")

    others = _known_others(known)
    if key in others:
        return None                                       # it is another customer we already know

    # Full company name followed by more words.
    for n in range(len(toks) - 1, 0, -1):
        head = _fold("".join(toks[:n]))
        for cust, cfg in SHEET_CUSTOMERS.items():
            if head in {_key(a) for a in cfg["full"]}:
                return Match(cust, "starts with")

    # One or two letters off a longer alias; never a different digit.
    best = []
    for cust, cfg in SHEET_CUSTOMERS.items():
        for alias in {_key(a) for a in [cust, cfg["folder"] or cust, *cfg["aliases"]]}:
            allowed = _allowed_typos(alias)
            if not allowed or _digits(alias) != _digits(key) or abs(len(alias) - len(key)) > allowed:
                continue
            d = _distance(key, alias)
            if 0 < d <= allowed:
                best.append((d, cust))
    if not best:
        return None
    best.sort()
    d, cust = best[0]
    if any(c != cust and dd == d for dd, c in best):
        return None                                       # two of ours are equally close
    if any(_distance(key, k) <= d for k in others if abs(len(k) - len(key)) <= d):
        return None                                       # another real customer is just as close
    return Match(cust, "spelling")


def canonical(name, known=None):
    """Canonical customer name ("GO2", "CDO", "Venture Research") or None."""
    m = match(name, known)
    return m.customer if m else None


def folder_for(customer) -> str:
    """The share folder a customer's jobs are filed under (its own name unless set above)."""
    cfg = SHEET_CUSTOMERS.get(customer)
    return (cfg and cfg["folder"]) or customer
