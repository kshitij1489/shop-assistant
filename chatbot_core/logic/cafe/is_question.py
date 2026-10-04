import re

# --- Core lexicons ------------------------------------------------------------

QUESTION_MARKS = {"?", "？"}  # ASCII + full-width

QUESTION_WORDS = {
    # WH-words (incl. multiword)
    "who","whom","whose","what","when","where","why","how","which",
    "howcome","how come","what about","how about","what if","how many",
    "how much","how long","how far","how often","how soon","since when"
}

AUX_STARTERS = {
    # Auxiliaries / modals that commonly start polar questions
    "is","are","am","was","were","do","does","did",
    "can","could","will","would","shall","should","may","might","must","ought",
    "have","has","had"
}

NEG_CONTRACTIONS = {
    # Useful for detecting negative questions like "isn't", "won't"
    "isn't","arent","aren't","amn't","wasn't","werent","weren't",
    "don't","doesn't","didn't","cant","can't","couldn't","won't","wont",
    "wouldn't","shan't","shouldn't","mayn't","mightn't","mustn't","oughtn't",
    "haven't","hasn't","hadn't"
}

# Phrases that usually *begin* a question/request even without "?"
LEADING_PHRASES = {
    # Direct requests
    "can you","could you","would you","will you","do you","did you","are you",
    "is it","is there","are there","have you","has anyone","should we","shall we",
    "could we","would we","can we","why don't we","why dont we","how about","what about",
    "is it possible","would it be possible","is there any way","any chance",
    "any update","any updates","any idea","any ideas","any thoughts","thoughts on",
    "mind if","would you mind","do you mind","care to",
    # Availability / inventory
    "are you available","are you free","do you have","have you got","got any",
    # Indirect but actionable
    "let me know","please confirm","please advise","please clarify",
    "please share","please provide","please respond","please reply",
    "kindly confirm","kindly advise","kindly clarify","kindly share","kindly provide",
    "please check","please verify","please approve","please review",
    # Ordering / choice questions phrased as offers
    "shall i","should i","can i","could i","may i","might i", "do i"
}

# Tag questions appended to a statement
TAG_QUESTIONS = {
    # Common tags (include variants without apostrophes)
    "right","correct","ok","okay","yeah","no",
    "isn't it","isnt it","aren't you","arent you","aren't we","arent we",
    "don't you","dont you","doesn't it","doesnt it","won't you","wont you",
    "wouldn't you","wouldnt you","shouldn't we","shouldnt we",
    "couldn't we","couldnt we","haven't you","havent you","hasn't he","hasnt he",
    "didn't we","didnt we","am i right","aren't i","arent i"
}

# Interrogative fragments that often appear alone
INTERROGATIVE_FRAGMENTS = {
    "status","update","eta","next steps","thoughts","opinion","ideas","feedback",
    "question","questions","which","when","where","why","how","what","price","cost",
    "details","example","examples","explain","clarify"
}

# Indirect-question patterns (regex) appearing anywhere in a sentence
INDIRECT_Q_PATTERNS = [
    r"\blet me know\b",
    r"\bplease\s+(confirm|advise|clarify|share|provide|respond|reply|review|check|verify|approve)\b",
    r"\bkindly\s+(confirm|advise|clarify|share|provide|respond|reply|review|check|verify|approve)\b",
    r"\bwould you mind\b",
    r"\bmind if\b",
    r"\bis it (at all )?possible\b",
    r"\bis there any way\b",
    r"\bcould you (please )?\b",
    r"\bcan you (please )?\b",
    r"\bshould we\b",
    r"\bshall we\b",
    r"\b(any|some)\s+(update|updates|chance|ideas|thoughts)\b",
]

# --- Helpers -----------------------------------------------------------------

_LEAD_RE = re.compile(
    r"^\s*(?:" +
    r"|".join(re.escape(p) for p in sorted(LEADING_PHRASES, key=len, reverse=True)) +
    r")\b", re.IGNORECASE
)

_TAG_RE = re.compile(
    r"(?:,?\s+)(?:" +
    r"|".join(re.escape(t) for t in sorted(TAG_QUESTIONS, key=len, reverse=True)) +
    r")\s*\.?\s*$",
    re.IGNORECASE
)

_WH_OR_AUX_RE = re.compile(
    r"^\s*(?:" +
    r"|".join(
        [r"(?:%s)\b" % re.escape(w) for w in sorted(QUESTION_WORDS | AUX_STARTERS, key=len, reverse=True)]
    ) + r")",
    re.IGNORECASE
)

_NEG_Q_RE = re.compile(
    r"^\s*(?:%s)\b" % r"|".join(re.escape(w) for w in sorted(NEG_CONTRACTIONS, key=len, reverse=True)),
    re.IGNORECASE
)

_FRAGMENT_RE = re.compile(
    r"^\s*(?:" +
    r"|".join(re.escape(p) for p in sorted(INTERROGATIVE_FRAGMENTS, key=len, reverse=True)) +
    r")\s*\??\s*$",
    re.IGNORECASE
)

_INDIRECT_RES = [re.compile(pat, re.IGNORECASE) for pat in INDIRECT_Q_PATTERNS]

_DECLARATIVE_WONDERING_RE = re.compile(
    r"\b(i|we)\s+(was|were|am|are|'m|'re)?\s*(just\s*)?(wondering|curious|thinking)\b",
    re.IGNORECASE
)

def _sentences(text: str):
    # Split on sentence punctuation (., !, ?, …) while keeping order
    for chunk in re.split(r"[.!?…]+", text):
        s = chunk.strip().strip('\'"“”‘’')
        if s:
            yield s

# --- Main API ----------------------------------------------------------------

def is_question(text: str) -> bool:
    """
    Return True if `text` looks like a question (including indirect requests, tags,
    fragments, and missing '?'), else False.
    """
    if not text or not text.strip():
        return False

    # 1) Any visible question mark symbol anywhere
    if any(ch in text for ch in QUESTION_MARKS):
        return True

    # 2) Sentence-wise heuristics (catch questions not using '?')
    for sent in _sentences(text):
        s = sent.strip()

        # WH / auxiliary / negative-contraction starters
        if _WH_OR_AUX_RE.search(s) or _NEG_Q_RE.search(s):
            return True

        # Leading request/offer phrases
        if _LEAD_RE.search(s):
            return True

        # Tag questions appended to a statement
        if _TAG_RE.search(s):
            return True

        # Interrogative fragments (e.g., "ETA", "Update")
        if _FRAGMENT_RE.match(s):
            return True

        # Indirect request patterns anywhere in the sentence
        if any(rx.search(s) for rx in _INDIRECT_RES):
            return True

        # Explicit non-questions like "I/we (am) wondering..." — skip (do not early-return False)
        if _DECLARATIVE_WONDERING_RE.search(s):
            continue

    return False
