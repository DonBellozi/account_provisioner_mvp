from __future__ import annotations

import re
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


@dataclass(frozen=True)
class ParsedPerson:
    last_name: str
    first_name: str
    middle_name: str
    personal_email: str


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def transliterate(value: str) -> str:
    out: list[str] = []
    for char in value.lower():
        if char in TRANSLIT:
            out.append(TRANSLIT[char])
        elif char.isascii() and (char.isalnum() or char in "-."):
            out.append(char)
    return "".join(out)


def parse_two_line_input(raw: str) -> ParsedPerson:
    lines = [normalize_spaces(line) for line in raw.splitlines() if normalize_spaces(line)]
    if len(lines) < 2:
        raise ValueError("Вставьте ФИО и личный email двумя строками")

    email_line = next((line for line in lines if "@" in line), "")
    fio_line = next((line for line in lines if line != email_line), "")
    if not email_line or not fio_line:
        raise ValueError("Не удалось определить строку с ФИО или email")

    try:
        email = validate_email(email_line, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise ValueError(f"Некорректный личный email: {exc}") from exc

    parts = fio_line.split(" ")
    if len(parts) < 2:
        raise ValueError("ФИО должно содержать как минимум фамилию и имя")

    return ParsedPerson(
        last_name=parts[0],
        first_name=parts[1],
        middle_name=" ".join(parts[2:]),
        personal_email=email,
    )


def build_login_candidates(last_name: str, first_name: str, middle_name: str = "") -> list[str]:
    last = transliterate(last_name)
    first = transliterate(first_name)
    middle = transliterate(middle_name)
    initials = (first[:1] + middle[:1]).lower()

    candidates = [f"{last}.{initials}" if initials else f"{last}.{first[:1]}"]
    candidates.append(f"{last}.{first}")
    if middle:
        candidates.append(f"{last}.{first}.{middle[:1]}")
    candidates.append(f"{first[:1]}.{last}")

    result: list[str] = []
    for candidate in candidates:
        cleaned = re.sub(r"[^a-z0-9.-]", "", candidate.lower()).strip(".-")
        if cleaned and cleaned not in result:
            result.append(cleaned[:20])  # sAMAccountName compatibility
    return result
