from _loader import auth_models

hash_password = auth_models.hash_password
verify_password = auth_models.verify_password


def test_hash_format():
    scheme, iterations, salt, digest = hash_password("correct horse").split("$")
    assert scheme == "pbkdf2_sha256"
    assert iterations == "200000"
    assert len(salt) == 32  # 16 random bytes, hex encoded
    assert len(digest) == 64  # sha256


def test_hash_is_salted():
    """Same password twice must not produce the same hash."""
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_accepts_correct_password():
    assert verify_password("s3cret-password", hash_password("s3cret-password"))


def test_verify_rejects_wrong_password():
    assert not verify_password("wrong-password", hash_password("s3cret-password"))


def test_verify_is_case_sensitive():
    assert not verify_password("S3CRET-PASSWORD", hash_password("s3cret-password"))


def test_verify_rejects_empty_against_real_hash():
    assert not verify_password("", hash_password("s3cret-password"))


def test_unicode_password_roundtrip():
    assert verify_password("лозинка-ђšč", hash_password("лозинка-ђšč"))


def test_verify_returns_false_on_malformed_stored_value():
    """Must not raise — a corrupt row should read as 'wrong password'."""
    for stored in ["", "not-a-hash", "a$b$c", "a$b$c$d$e", "pbkdf2_sha256$x$yy$zz"]:
        assert verify_password("anything", stored) is False
