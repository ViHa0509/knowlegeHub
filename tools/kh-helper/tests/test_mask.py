from kh_helper.mask import CENSORED, mask


def test_keyed_values():
    t, n = mask("my password: hunter22 and token=abcd1234")
    assert "hunter22" not in t and "abcd1234" not in t and n == 2
    assert t.count(CENSORED) == 2


def test_plain_words_and_short_values_kept():
    t, n = mask("the password is expired; pwd: abc")
    assert t == "the password is expired; pwd: abc" and n == 0


def test_tokens_bearer_url_pem():
    text = ("use sk-abcdefghijklmnop and ghp_ABCDEFGHIJKLMNOPQRST, Bearer xyzxyzxyz, "
            "postgres://bob:s3cretpw@db:5432/x, AKIAABCDEFGHIJKLMNOP")
    t, n = mask(text)
    for s in ("sk-abcdefghijklmnop", "ghp_ABCDEFGHIJKLMNOPQRST", "xyzxyzxyz", "s3cretpw", "AKIAABCDEFGHIJKLMNOP"):
        assert s not in t
    assert "bob:" + CENSORED + "@db" in t
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    t2, n2 = mask("key " + pem)
    assert "MIIabc" not in t2 and n2 == 1


def test_jwt_and_sha_untouched():
    t, _ = mask("token eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl here; commit 41f756bce309a0deadbeef")
    assert "eyJhbGciOi" not in t
    assert "41f756bce309a0deadbeef" in t


def test_vietnamese_separator():
    t, n = mask("mật khẩu là Abc12345")
    assert "Abc12345" not in t and n == 1
