import argparse

from sqlalchemy import select

from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.entities import User


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or rotate the single local operator account for the Semantic MVP."
    )
    parser.add_argument("--email", required=True, help="Operator email to set on the local account")
    parser.add_argument("--password", required=True, help="New plaintext password for the local account")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    email = args.email.strip().lower()
    password = args.password

    if not email:
        raise SystemExit("Email must not be empty")

    with SessionLocal() as db:
        users = db.execute(select(User).order_by(User.created_at)).scalars().all()
        if len(users) > 1:
            raise SystemExit("Expected at most one user account in MVP mode; found multiple users")

        if users:
            user = users[0]
            action = "rotated"
            user.email = email
            user.password_hash = get_password_hash(password)
        else:
            action = "created"
            user = User(email=email, password_hash=get_password_hash(password))
            db.add(user)

        db.add(user)
        db.commit()
        db.refresh(user)

    print(f"{action} local operator account: {user.email} ({user.id})")


if __name__ == "__main__":
    main()