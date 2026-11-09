from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_cipher: Fernet | None = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        _cipher = Fernet(get_settings().fernet_key.encode("ascii"))
    return _cipher


def encrypt(plaintext: str) -> str:
    return _get_cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _get_cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("unable to decrypt stored secret") from exc


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-2:]}"
