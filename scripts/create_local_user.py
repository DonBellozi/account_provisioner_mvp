from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import LocalUser, UserRole
from app.security import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать или обновить локального оператора")
    parser.add_argument("username")
    parser.add_argument("--role", choices=[role.value for role in UserRole], default=UserRole.OPERATOR.value)
    args = parser.parse_args()

    password = getpass.getpass("Пароль: ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if password != confirmation:
        raise SystemExit("Пароли не совпадают")
    if len(password) < 12:
        raise SystemExit("Локальный пароль должен быть не короче 12 символов")

    with SessionLocal() as db:
        user = db.scalar(select(LocalUser).where(LocalUser.username == args.username))
        if user:
            user.password_hash = hash_password(password)
            user.role = UserRole(args.role)
            user.is_active = True
            action = "обновлен"
        else:
            db.add(
                LocalUser(
                    username=args.username,
                    password_hash=hash_password(password),
                    role=UserRole(args.role),
                )
            )
            action = "создан"
        db.commit()
    print(f"Локальный пользователь {args.username} {action}")


if __name__ == "__main__":
    main()
