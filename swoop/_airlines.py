"""Native airline exclusion intent, independent of a truncated result roster."""
from typing import Optional


def normalize_exclusions(excluded: Optional[list[str]], included: Optional[list[str]] = None) -> Optional[list[str]]:
    if excluded is None:
        return None
    if not isinstance(excluded, list) or any(not isinstance(code, str) or len(code) != 2
            or not code.isascii() or not code.isalnum() for code in excluded):
        raise ValueError('exclude_airlines requires two-character airline codes')
    result = list(dict.fromkeys(code.upper() for code in excluded))
    if included and all(isinstance(code, str) and len(code) == 2 for code in included) \
            and all(code.upper() in result for code in included):
        raise ValueError('airline exclusions leave no included airlines')
    return result or None
