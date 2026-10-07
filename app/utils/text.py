"""Small text helpers used by request validation."""

import unicodedata


def normalize_text(value: object) -> object:
    """Trim and collapse whitespace, use composed Unicode (NFC), reject invalid characters.

    NFC turns "e" + combining accent into the single character "é", so the same visible
    text is always stored (and rendered) the same way.

    Used as a Pydantic "before" validator, so non-string input is returned unchanged
    and Pydantic's own type check reports it.
    """
    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    # Cc = control characters, Cs = lone surrogates (cannot be stored as UTF-8).
    if any(unicodedata.category(char) in ("Cc", "Cs") for char in normalized):
        raise ValueError("must not contain control or invalid characters")
    return normalized
