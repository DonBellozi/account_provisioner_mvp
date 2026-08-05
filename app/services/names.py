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

MAX_LOGIN_LENGTH = 20
RUSSIAN_NAME_RE = re.compile(r"^[А-Яа-яЁё]+(?:[ -][А-Яа-яЁё]+)*$")


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


def validate_russian_name(value: str, field_name: str, *, required: bool = True) -> str:
    """Validate that a name contains only Russian letters, spaces and hyphens.

    This intentionally rejects Latin look-alike characters such as a/c/e/o/p/x,
    because they are a common source of incorrect AD attributes and logins.
    """
    normalized = normalize_spaces(value)
    if not normalized:
        if required:
            raise ValueError(f"Поле «{field_name}» не заполнено")
        return ""
    if not RUSSIAN_NAME_RE.fullmatch(normalized):
        suspicious = sorted({char for char in normalized if not re.fullmatch(r"[А-Яа-яЁё -]", char)})
        details = ", ".join(repr(char) for char in suspicious) or "неподдерживаемые символы"
        raise ValueError(
            f"В поле «{field_name}» обнаружены символы не из русской раскладки: {details}. "
            "Разрешены русские буквы, пробел и дефис."
        )
    return normalized


def validate_person_name(last_name: str, first_name: str, middle_name: str = "") -> tuple[str, str, str]:
    return (
        validate_russian_name(last_name, "Фамилия"),
        validate_russian_name(first_name, "Имя"),
        validate_russian_name(middle_name, "Отчество", required=False),
    )


def parse_two_line_input(raw: str) -> ParsedPerson:
    """Разобрать ФИО и необязательный личный email.

    Поддерживаются два варианта:
    - одна строка с ФИО;
    - две строки с ФИО и email в любом порядке.
    """
    lines = [
        normalize_spaces(line)
        for line in raw.splitlines()
        if normalize_spaces(line)
    ]
    if not lines:
        raise ValueError(
            "Введите ФИО одной строкой и при наличии личный email второй строкой"
        )

    email_lines = [line for line in lines if "@" in line]
    if len(email_lines) > 1:
        raise ValueError("Обнаружено несколько строк с email")

    email = ""
    if email_lines:
        email_line = email_lines[0]
        fio_lines = [line for line in lines if line != email_line]
        if len(fio_lines) != 1:
            raise ValueError(
                "Не удалось однозначно определить строку с ФИО"
            )
        fio_line = fio_lines[0]
        try:
            email = validate_email(
                email_line,
                check_deliverability=False,
            ).normalized
        except EmailNotValidError as exc:
            raise ValueError(f"Некорректный личный email: {exc}") from exc
    else:
        if len(lines) != 1:
            raise ValueError(
                "Без личного email ФИО необходимо ввести одной строкой"
            )
        fio_line = lines[0]

    parts = fio_line.split(" ")
    if len(parts) < 2:
        raise ValueError("ФИО должно содержать как минимум фамилию и имя")

    last_name, first_name, middle_name = validate_person_name(
        parts[0],
        parts[1],
        " ".join(parts[2:]),
    )
    return ParsedPerson(
        last_name=last_name,
        first_name=first_name,
        middle_name=middle_name,
        personal_email=email,
    )


def _candidate(last: str, suffix: str | None = None) -> str:
    last = re.sub(r"[^a-z0-9-]", "", last.lower())
    if not suffix:
        return last[:MAX_LOGIN_LENGTH].strip(".-")

    suffix = re.sub(r"[^a-z0-9-]", "", suffix.lower())
    available_for_last = MAX_LOGIN_LENGTH - len(suffix) - 1
    if available_for_last < 1:
        return ""
    return f"{last[:available_for_last]}.{suffix}".strip(".-")


def build_login_candidates(last_name: str, first_name: str, middle_name: str = "") -> list[str]:
    """Build login candidates in the organization's required order.

    Order:
    1. surname + first-name initial + patronymic initial, e.g. ivanov.ii;
    2. surname + first-name initial, e.g. ivanov.i;
    3. surname only, e.g. ivanov;
    4. letter-expansion variants without numeric suffixes.

    Expansion uses the transliterated spelling. Therefore the patronymic
    «Юрьевич» first contributes ``y`` and then expands to ``yu``, ``yur`` etc.
    """
    last = transliterate(last_name)
    first = transliterate(first_name)
    middle = transliterate(middle_name)
    if not last or not first:
        raise ValueError("Не удалось сформировать логин из ФИО")

    result: list[str] = []

    def add(value: str) -> None:
        cleaned = re.sub(r"[^a-z0-9.-]", "", value.lower()).strip(".-")
        if cleaned and len(cleaned) <= MAX_LOGIN_LENGTH and cleaned not in result:
            result.append(cleaned)

    first_initial = first[:1]
    middle_initial = middle[:1]

    # Основные варианты строго в согласованном порядке.
    add(_candidate(last, first_initial + middle_initial if middle_initial else first_initial))
    add(_candidate(last, first_initial))
    add(_candidate(last))

    # Сначала расширяем отчество: y -> yu -> yur ...
    if middle:
        for length in range(2, min(len(middle), 8) + 1):
            add(_candidate(last, first_initial + middle[:length]))

    # Затем расширяем имя, сохраняя первую букву отчества.
    if middle_initial:
        for length in range(2, min(len(first), 8) + 1):
            add(_candidate(last, first[:length] + middle_initial))

    # Варианты только с расширенным именем.
    for length in range(2, min(len(first), 8) + 1):
        add(_candidate(last, first[:length]))

    # Дополнительные комбинированные варианты для редких серий совпадений.
    if middle:
        for first_length in range(2, min(len(first), 5) + 1):
            for middle_length in range(2, min(len(middle), 5) + 1):
                add(_candidate(last, first[:first_length] + middle[:middle_length]))

    return result
