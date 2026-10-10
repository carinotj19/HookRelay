from hookrelay.security import sign, validate_destination_url, verify


def test_signature_covers_raw_body_and_timestamp():
    secret = "secret"
    sig = sign(secret, b'{"ok":true}', 1_700_000_000)
    assert verify(secret, b'{"ok":true}', sig, 300, now=1_700_000_000)
    assert not verify(secret, b'{"ok":false}', sig, 300, now=1_700_000_000)
    assert not verify(secret, b'{"ok":true}', sig, 300, now=1_700_000_301)
    assert not verify("wrong", b'{"ok":true}', sig, 300, now=1_700_000_000)
    assert not verify(secret, b'{"ok":true}', "t=1,v1=hi,v1=hi", 300, now=1)


def test_url_validation_rejects_untrusted_literal_hosts():
    for url in ["http://example.com/x", "https://127.0.0.1/x", "https://[::1]/x",
                "https://localhost/x", "https://user:pass@example.com/x"]:
        try:
            validate_destination_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Should reject {url}")
    assert validate_destination_url("https://example.com/webhook") == "https://example.com/webhook"
