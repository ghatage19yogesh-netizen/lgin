"""Simple username/password login with two roles: planner and approver.

Users come from Streamlit secrets when present ([users.<username>] blocks with
name, role, salt, password_hash), otherwise from the demo accounts below.
Passwords are stored only as salted PBKDF2 hashes.

This is demo-grade auth. For production use your company SSO (e.g. Streamlit's
st.login with an OIDC provider such as Okta / Azure AD / Google).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets

ROLES = ("planner", "approver")


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def make_user(name: str, role: str, password: str) -> dict:
    salt = pysecrets.token_hex(16)
    return {"name": name, "role": role, "salt": salt, "password_hash": hash_password(password, salt)}


# Demo accounts (password shown on the login page only while these defaults are in use)
_DEMO_SALT_1 = "8f1d2c3b4a5968778695a4b3c2d1e0f1"
_DEMO_SALT_2 = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
DEMO_USERS = {
    "maria": {"name": "Maria Lopez", "role": "planner", "salt": _DEMO_SALT_1,
              "password_hash": hash_password("planner123", _DEMO_SALT_1)},
    "david": {"name": "David Brooks", "role": "approver", "salt": _DEMO_SALT_2,
              "password_hash": hash_password("approver123", _DEMO_SALT_2)},
}
DEMO_PASSWORDS = {"maria": "planner123", "david": "approver123"}


def load_users(st_secrets) -> tuple[dict, bool]:
    """Return (users, using_demo_accounts)."""
    try:
        users = {k: dict(v) for k, v in st_secrets["users"].items()}
        if users:
            return users, False
    except Exception:
        pass
    return DEMO_USERS, True


def check_login(users: dict, username: str, password: str) -> dict | None:
    u = users.get((username or "").strip().lower())
    if not u or u.get("role") not in ROLES:
        # still burn time to avoid a timing hint
        hash_password(password or "", "00" * 16)
        return None
    if hmac.compare_digest(hash_password(password or "", u["salt"]), u["password_hash"]):
        return {"username": username.strip().lower(), "name": u["name"], "role": u["role"]}
    return None


if __name__ == "__main__":
    # Helper: python -m allocation.auth <username> "<Full Name>" <role> <password>
    import json
    import sys
    if len(sys.argv) != 5:
        print('usage: python -m allocation.auth <username> "<Full Name>" <planner|approver> <password>')
        sys.exit(1)
    user, name, role, pw = sys.argv[1:]
    u = make_user(name, role, pw)
    print(f"[users.{user}]")
    for k, v in u.items():
        print(f"{k} = {json.dumps(v)}")
