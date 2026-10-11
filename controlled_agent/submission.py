"""Bounded, pure structural parsing; no identity, policy, or tool semantics."""

import json
from math import isfinite


class MalformedProposal(ValueError):
    """Fixed diagnostic; never includes proposal data."""


def parse_proposal(proposal: object) -> tuple[str, dict]:
    """Copy only a plain tool_name/arguments envelope with flat JSON scalars.

    Maximum: 128-character tool/keys, 256-character string values, eight
    arguments, signed 64-bit integers, finite floats, 4096 encoded JSON bytes.
    No custom mapping/string hooks or recursive traversal are permitted.
    """
    def invalid():
        raise MalformedProposal("Malformed proposal")

    if type(proposal) is not dict or len(proposal) != 2:
        invalid()
    if any(type(key) is not str for key in proposal):
        invalid()
    if set(proposal) != {"tool_name", "arguments"}:
        invalid()
    tool = proposal["tool_name"]
    arguments = proposal["arguments"]
    if type(tool) is not str or not 1 <= len(tool) <= 128 or not tool.strip():
        invalid()
    if type(arguments) is not dict or len(arguments) > 8:
        invalid()
    copied = {}
    for key, value in arguments.items():
        if type(key) is not str or not 1 <= len(key) <= 128:
            invalid()
        kind = type(value)
        if kind is str:
            if len(value) > 256:
                invalid()
        elif kind is int:
            if not -(2 ** 63) <= value < 2 ** 63:
                invalid()
        elif kind is float:
            if not isfinite(value):
                invalid()
        elif value is not None and kind is not bool:
            invalid()
        copied[key] = value
    try:
        encoded = json.dumps({"tool_name": tool, "arguments": copied},
                             ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError):
        invalid()
    if len(encoded) > 4096:
        invalid()
    return tool, copied
