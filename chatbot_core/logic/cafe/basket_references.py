"""Explicit basket row references shared by interpretation and validation."""
import re


# A row number is distinct from an item quantity, size, or product identifier.
# Keep numeric boundaries strict: "item 1.5" must not authorize row 1.
BASKET_REFERENCE = re.compile(
    r"(?<!\w)(?:(?:item|entry|line)\s+(?:number\s+)?#?\s*|#\s*)"
    r"(?P<number>[0-9]+)(?!\w|\.\d|/\d)",
    re.I,
)
