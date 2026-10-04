"""Lexical fit between a customer's words and catalog product names. Pure functions.

Words are compared after a small catalog-notation normalization: number words
become digits and explicit package counts become single tokens, so "two pieces"
fits "(2pcs)" without matching an unrelated order quantity. A product name has a
*head segment* (the words before the first connector such as "with" or "and") that names what the
product is, and a *component segment* that lists what comes with it. The
connectors themselves are never evidence.
"""
import re

CONNECTORS = frozenset({'with', 'and', 'plus'})
NUMBER_WORDS = {
    'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
    'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12',
}
COUNT_UNITS = {'piece': 'pcs', 'pieces': 'pcs', 'pc': 'pcs', 'pcs': 'pcs'}
_TOKEN = re.compile(r"\d+|[^\W\d_]+")
_PACKAGE = re.compile(r"\d+pcs")


def tokens(value) -> list[str]:
    """Normalized words of a name or message, connectors included."""
    text = str(value).casefold().replace('&', ' and ')
    words = [COUNT_UNITS.get(NUMBER_WORDS.get(token, token), NUMBER_WORDS.get(token, token))
             for token in _TOKEN.findall(text)]
    return re.sub(r"\b(\d+) pcs\b", r"\1pcs", ' '.join(words)).split()


def match_words(value) -> list[str]:
    """Words that may serve as evidence for a product."""
    return [token for token in tokens(value) if token not in CONNECTORS]


def head_segment(name) -> set[str]:
    """Words naming what the product is, before any connector."""
    words = tokens(name)
    for index, word in enumerate(words):
        if word in CONNECTORS:
            return set(words[:index])
    return set(words)


def _phrases(row):
    return [row['name'], *row.get('aliases', [])]


def _contains_phrase(text_words, phrase_words):
    """The phrase appears as consecutive whole words of the text."""
    size = len(phrase_words)
    return size > 0 and any(text_words[start:start + size] == phrase_words
                            for start in range(len(text_words) - size + 1))


def _exact_rivals(rows, item_id, text_words, exact):
    """A fully named product competes only with an identical alias or a longer name containing it."""
    def competes(phrase_words):
        return _contains_phrase(text_words, phrase_words) and any(
            phrase_words == match or set(match) < set(phrase_words) for match in exact)
    return [row for key, row in rows.items() if key != str(item_id)
            and any(competes(match_words(phrase)) for phrase in _phrases(row))]


def _matched_words(phrase, text_words):
    """A package count is evidence only beside wording naming this product.

    Keep connectors in the message here: "brownie and two pieces of cake"
    must not attach the cake's package count to the brownie.
    """
    phrase_words = set(match_words(phrase))
    evidence = phrase_words & set(text_words)
    packages = {word for word in phrase_words if _PACKAGE.fullmatch(word)}
    for package in evidence & packages:
        if not any(_contains_phrase(text_words, pattern)
                   for word in phrase_words - packages
                   for pattern in ([word, package], [package, word],
                                   [package, 'of', word], [package, 'of', 'the', word])):
            evidence.remove(package)
    return evidence


def _partial_rivals(rows, item_id, chosen, text_words):
    """A partial name competes with every product carrying all of its matched words.

    The proposal stands undisputed when every matched word names the chosen
    product's head and reaches the rivals only through their component words.
    """
    evidence = max((_matched_words(phrase, text_words) for phrase in _phrases(chosen)),
                   key=len, default=set())
    if not evidence:
        return []
    rivals = [row for key, row in rows.items() if key != str(item_id)
              and any(evidence <= set(match_words(phrase)) for phrase in _phrases(row))]
    names_chosen_head = any(evidence <= head_segment(phrase) for phrase in _phrases(chosen))
    touches_rival_head = any(evidence & head_segment(phrase)
                             for row in rivals for phrase in _phrases(row))
    if names_chosen_head and not touches_rival_head:
        return []
    return rivals


def competing_catalog_items(text, item_id, catalog):
    """Catalog rows the customer's wording fits as well as the proposed item.

    Without any lexical overlap the proposal stands undisputed.
    """
    rows = {str(row['id']): row for row in catalog}
    chosen = rows.get(str(item_id))
    if chosen is None:
        return []
    text_words = match_words(text)
    exact = [match_words(phrase) for phrase in _phrases(chosen)
             if _contains_phrase(text_words, match_words(phrase))]
    if exact:
        return _exact_rivals(rows, item_id, text_words, exact)
    return _partial_rivals(rows, item_id, chosen, tokens(text))
