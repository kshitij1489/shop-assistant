MODIFIERS = [
    "with almond milk", "with oat milk", "with soy milk", "with coconut milk",
    "with extra shot", "extra shot", "double shot",
    "less sugar", "no sugar", "sugar free", "more sugar", "extra sweet", "less sweet",
    "no toppings", "with toppings", "extra toppings",
    "with chocolate syrup", "with caramel syrup",
    "with nuts", "without nuts",
    "with sprinkles", "with whipped cream", "no whipped cream", "no cream",
    "with sauce", "without sauce"
]

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10
}

SIZE_KEYWORDS = [
    "size", "sized", "big", "small", "large", "regular", "family",
    "mini", "scoop", "mini tub", "tub", "cup", "cone",
    "single", "double", 
    "medium", "grande", "sharing", "for one", "for two", "for three", "for four"
]

QUANTITY_KEYWORDS = [
    "quantity", "number", "count", "how many", "qty", "unit", 
    "amount", "piece", "serving", "portion", 
    "how much", "how many scoops", "how many tubs", "how many cups"
]

ITEM_KEYWORDS = [
    "flavor", "item", "ice cream", "icecream", "sundae", 
    "dessert", "order", "selection", "type", "kind", 
    "which ice cream", "which flavor", "what flavor", "choose", "pick"
]

REQUEST_PHRASE_PATTERNS = [
    # polite short tokens
    r"\bplease\b",
    r"\bpls\b",

    # please + verb
    r"\bplease(?:\s+(?:add|give|include|order|send|put|drop|reserve|book|purchase|buy))\b",

    # explicit "add" forms
    r"\bto add\b",
    r"\badd\b",
    r"\badd(?:\s+(?:me|one|a|an|\d+|this|that))\b",
    r"\badd on\b",
    r"\badd(?:\s+it)?\s+to my (?:cart|order|bag)\b",
    r"\badd(?:\s+it)?\b",

    # cart / basket phrases
    r"\badd to cart\b",
    r"\bput (?:it|this|that)?\s*(?:in|into|on)\s*(?:my\s+)?(?:cart|basket|bag|order)\b",
    r"\bput me down for\b",
    r"\bput me on the list for\b",

    # "order"/request verbs and variants
    r"\border\b",
    r"\bplease\s+order\b",
    r"\border(?:\s+me)?\b",
    r"\bget me\b",
    r"\bgive me\b",
    r"\bcan you\b",
    r"\bcould you\b",
    r"\bcould you(?:\s+please)?(?:\s+add)?\b",
    r"\bcan i have\b",
    r"\bmay i have\b",
    r"\bcan i get\b",
    r"\bcan you get\b",
    r"\bcan you add\b",
    r"\bcould i get\b",
    r"\bi(?:'|’)?d like to order\b",
    r"\bi(?:'|’)?d like\b",

    # first-person desire / polite forms (with contractions)
    r"\bi want\b",
    r"\bi want to\b",
    r"\bi need\b",
    r"\bi(?:'|’)?d like\b",
    r"\bi(?:'|’)?d like to\b",
    r"\bi would like\b",
    r"\bi would like to\b",
    r"\bi(?:'|’)?ll take\b",
    r"\bi(?:'|’)?ll have\b",
    r"\bi(?:'|’)?d love\b",
    r"\bi(?:'|’)?d love to\b",
    r"\bi(?:'|’)?d love one\b",
    r"\bi(?:'|’)?ll get\b",
    r"\bi(?:'|’)?ll take one\b",

    # shorter/ambiguous "want" family
    r"\bwant to add\b",
    r"\bwant\b",
    r"\bwanna\b",
    r"\bhit me with\b",
    r"\bgrab me\b",
    r"\bgrab(?:\s+one)?\b",
    r"\bget(?:\s+one)?\b",

    # buying/purchasing language
    r"\bbuy\b",
    r"\bpurchase\b",
    r"\bcharge me\b",
    r"\bbill me\b",

    # casual conversational phrases
    r"\bput it on my tab\b",
    r"\bput it on the tab\b",
    r"\bput it on (?:my )?bill\b",
    r"\bcount me in for\b",
    r"\bcount me in\b",
    r"\bcount me(?:\s+one)?\b",

    # add-more / repeat / increment requests
    r"\bone more\b",
    r"\badd one more\b",
    r"\banother\b",
    r"\bagain\b",
    r"\bplus(?:\s+one)?\b",
    r"\bplus\b",
    r"\bmore\b",

    # colloquial shorthand
    r"\bpls add\b",
    r"\bpls order\b",
    r"\bthrew in\b",
    r"\bthrow in\b",
    r"\bslap on\b",

    # safety: short polite fragments that often precede items
    r"\bcan you please\b",
    r"\bcould you please\b",
    r"\bplease could you\b",
    r"\bwould you\b",
    r"\bwould you mind\b"
]

NEGATION_PATTERNS = [
    r"\bno\b",
    r"\bnot\b",
    r"\bnone\b",
    r"\bnothing\b",
    r"\bneither\b",
    r"\bnever\b",
    r"\bnowhere\b",
    r"\bwithout\b",
    r"\bexcept\b",
    r"\bskip\b",
    r"\bavoid\b",
    r"\bdo\s*not\b",
    r"\bdon't\b",
    r"\bdoes\s*not\b",
    r"\bdoesn't\b",
    r"\bdid\s*not\b",
    r"\bdidn't\b",
    r"\bcan't\b",
    r"\bcannot\b",
    r"\bwon't\b",
    r"\bwould\s*not\b",
    r"\bwouldn't\b",
    r"\bcould\s*not\b",
    r"\bcouldn't\b",
    r"\bshould\s*not\b",
    r"\bshouldn't\b",
    r"\bwas\s*not\b",
    r"\bwasn't\b",
    r"\bwere\s*not\b",
    r"\bweren't\b",
    r"\bain't\b",  # Informal
    r"\bno\s+[a-z]+",  # e.g., no vanilla, no sugar
    r"\bnot\s+[a-z]+",  # e.g., not chocolate
    r"\bdon't\s+include\b",
    r"\bkeep\s+.*\bout\b",  # e.g., "keep nuts out"
    r"\bleave\s+.*\bout\b",  # e.g., "leave out strawberry"
    r"\bi\s+don’t\s+want\b",
    r"\bi\s+don’t\s+like\b",
    r"\bi\s+don’t\s+prefer\b",
    r"\bi\s+hate\b",
    r"\bi\s+dislike\b"
]