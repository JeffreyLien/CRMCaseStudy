"""Deterministic facility/account matching helpers.

Address and geography deliberately outweigh name similarity.  The module has no
CRM side effects and uses only the Python standard library.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_STREET_WORDS = {
    "avenue": "ave", "av": "ave", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "road": "rd", "street": "st", "highway": "hwy",
    "parkway": "pkwy", "place": "pl", "court": "ct", "circle": "cir", "pk": "pike",
    "terrace": "ter", "trail": "trl", "route": "rte", "pike": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def normalize_name(value: object) -> str:
    words = normalize_text(value).split()
    aliases = {"centre": "center", "healthcare": "health", "rehabilitation": "rehab", "and": ""}
    return " ".join(aliases.get(word, word) for word in words if aliases.get(word, word))


def normalize_address(value: object) -> str:
    words = normalize_text(value).split()
    # Keep PO boxes recognizable, but do not let them equal a physical address.
    return " ".join(_STREET_WORDS.get(word, word) for word in words)


def normalize_city(value: object) -> str:
    return normalize_text(value)


def normalize_state(value: object) -> str:
    return re.sub(r"[^A-Z]", "", str(value or "").upper())


def normalize_zip(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5]


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def compare(facility: dict, account: dict) -> dict:
    """Return explainable deterministic signals and a 0-100 match score."""
    wa, ca = normalize_address(facility.get("address")), normalize_address(account.get("billing_street"))
    wc, cc = normalize_city(facility.get("city")), normalize_city(account.get("billing_city"))
    ws, cs = normalize_state(facility.get("state")), normalize_state(account.get("billing_state"))
    wz, cz = normalize_zip(facility.get("zip")), normalize_zip(account.get("billing_zip"))
    wn, cn = normalize_name(facility.get("name")), normalize_name(account.get("name"))

    address_exact = bool(wa and wa == ca)
    city_exact = bool(wc and wc == cc)
    state_exact = bool(ws and ws == cs)
    zip_exact = bool(wz and wz == cz)
    name_similarity = _ratio(wn, cn)
    address_similarity = _ratio(wa, ca)

    # Hard geographic contradictions prevent a familiar name from winning.
    geo_conflict = bool((ws and cs and ws != cs) or (wz and cz and wz != cz and not address_exact))
    if address_exact:
        score = 72 + 8 * city_exact + 8 * state_exact + 7 * zip_exact + 5 * name_similarity
    elif city_exact and state_exact and zip_exact:
        score = 42 + 33 * address_similarity + 25 * name_similarity
    elif city_exact and state_exact:
        score = 30 + 30 * address_similarity + 30 * name_similarity + 10 * zip_exact
    else:
        score = 18 * address_similarity + 22 * name_similarity + 8 * state_exact + 5 * zip_exact
    if geo_conflict:
        score = min(score, 49.0)

    return {
        "score": round(min(100.0, score), 2),
        "address_exact": address_exact, "address_similarity": round(address_similarity, 3),
        "city_exact": city_exact, "state_exact": state_exact, "zip_exact": zip_exact,
        "name_similarity": round(name_similarity, 3), "geo_conflict": geo_conflict,
        "normalized_address": wa, "normalized_crm_address": ca,
    }


def rank_candidates(facility: dict, accounts: list[dict]) -> list[tuple[dict, dict]]:
    ranked = [(account, compare(facility, account)) for account in accounts]
    return sorted(ranked, key=lambda item: (-item[1]["score"], item[0].get("account_id", "")))
