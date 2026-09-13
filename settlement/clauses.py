)
_FALLBACK = (
    re.compile(
        r"\bfall(?:s|ing)? back to (?:(?:the|a) )?"
        r"(?:last|prior|previous|most recent) (?:available )?"
        r"(?:month|period|release|observation|data)\b",
        re.IGNORECASE,
