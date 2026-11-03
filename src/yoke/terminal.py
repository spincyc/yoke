"""Render untrusted text safely in line-oriented terminal output.

This shared presentation helper has no control- or media-plane dependencies.
Cell-based terminal layouts may apply their own stricter ASCII policy.
"""

import unicodedata


def escape_terminal(value: str) -> str:
    """Make untrusted text inert on a terminal while preserving readable text."""
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
    rendered: list[str] = []
    for character in value:
        if unicodedata.category(character) not in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            rendered.append(character)
            continue
        code = ord(character)
        if character in escapes:
            rendered.append(escapes[character])
        elif code < 0x100:
            rendered.append(f"\\x{code:02x}")
        elif code < 0x10000:
            rendered.append(f"\\u{code:04x}")
        else:
            rendered.append(f"\\U{code:08x}")
    return "".join(rendered)
