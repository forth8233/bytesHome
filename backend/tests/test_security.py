from app.core.security import ACCESS, create_access_token, decode_token, hash_password, verify_password


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_access_token_roundtrip() -> None:
    token, jti, _expires = create_access_token(42)
    claims = decode_token(token, ACCESS)
    assert claims["sub"] == "42"
    assert claims["jti"] == jti
