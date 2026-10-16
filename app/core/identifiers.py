import re

_WHITESPACE = re.compile(r"\s+")


def normalize_employee_id(value: str) -> str:

    return _WHITESPACE.sub(" ", value).strip().upper()