"""Tiny JSON path resolver used by config-driven mappings.

Syntax: dotted keys, `[N]` for a list index, `[*]` to map over a list, e.g.
`data.synopsis.applicantTypes[*].description`. `$url` refers to the document URL.
"""

import re
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+|\*)\]")


def resolve(data: Any, path: str) -> Any:
    values: list[Any] = [data]
    fanned_out = False
    for key, index in _TOKEN.findall(path):
        next_values: list[Any] = []
        for value in values:
            if key:
                if isinstance(value, dict) and key in value:
                    next_values.append(value[key])
            elif index == "*":
                if isinstance(value, list):
                    next_values.extend(value)
                    fanned_out = True
            elif isinstance(value, list) and int(index) < len(value):
                next_values.append(value[int(index)])
        values = next_values
    if fanned_out:
        return [v for v in values if v not in (None, "")]
    return values[0] if values else None


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
