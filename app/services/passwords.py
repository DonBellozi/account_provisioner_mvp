from __future__ import annotations

import secrets
import string
from dataclasses import dataclass


@dataclass(frozen=True)
class PasswordRules:
    mail_length: int = 16
    mail_specials: str = "!@#$%&?"
    ad_min_length: int = 8
    ad_max_length: int = 12
    ad_specials: str = "!@#$%&?"


def _secure_shuffle(items: list[str]) -> str:
    rng = secrets.SystemRandom()
    rng.shuffle(items)
    return "".join(items)


def generate_mail_password(length: int = 16, specials: str = "!@#$%&?") -> str:
    if length < 8:
        raise ValueError("Длина почтового пароля должна быть не менее 8 символов")
    if not specials:
        raise ValueError("Нужен хотя бы один разрешенный спецсимвол")

    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    alphabet = string.ascii_letters + string.digits + specials
    required.extend(secrets.choice(alphabet) for _ in range(length - len(required)))
    return _secure_shuffle(required)


def _normalize_latin_letters(value: str) -> str:
    return "".join(ch for ch in value if ch in string.ascii_letters).lower()


def generate_ad_password(
    first_name_latin: str,
    last_name_latin: str,
    min_length: int = 8,
    max_length: int = 12,
    specials: str = "!@#$%&?",
) -> str:
    """Generate a temporary AD password from name fragments.

    The result always contains upper/lower Latin letters, two digits and one
    configured special character. The order and fragment lengths are random.
    """
    first = _normalize_latin_letters(first_name_latin)
    last = _normalize_latin_letters(last_name_latin)
    if not first or not last:
        raise ValueError("Для пароля AD нужны имя и фамилия после транслитерации")
    if min_length < 8 or max_length > 12 or min_length > max_length:
        raise ValueError("Диапазон пароля AD должен находиться в пределах 8–12 символов")
    if not specials:
        raise ValueError("Нужен хотя бы один разрешенный спецсимвол")

    rng = secrets.SystemRandom()
    for _ in range(200):
        first_len = rng.randint(min(3, len(first)), min(5, len(first))) if len(first) >= 3 else len(first)
        last_len = rng.randint(min(2, len(last)), min(4, len(last))) if len(last) >= 2 else len(last)

        first_part = first[:first_len]
        last_part = last[:last_len]
        if rng.choice([True, False]):
            pieces = [first_part.capitalize(), last_part.capitalize()]
        else:
            pieces = [last_part.capitalize(), first_part.capitalize()]

        digits = f"{rng.randrange(0, 100):02d}"
        special = rng.choice(specials)
        candidate = "".join(pieces) + digits + special

        if len(candidate) > max_length:
            excess = len(candidate) - max_length
            while excess > 0 and len(pieces[0]) > 2:
                pieces[0] = pieces[0][:-1]
                excess -= 1
            while excess > 0 and len(pieces[1]) > 2:
                pieces[1] = pieces[1][:-1]
                excess -= 1
            candidate = "".join(pieces) + digits + special

        while len(candidate) < min_length:
            insert = rng.choice(string.ascii_lowercase)
            candidate = candidate[:-3] + insert + candidate[-3:]

        if min_length <= len(candidate) <= max_length:
            if (
                any(c.isupper() for c in candidate)
                and any(c.islower() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(c in specials for c in candidate)
            ):
                return candidate

    raise RuntimeError("Не удалось сгенерировать пароль AD по заданным правилам")
