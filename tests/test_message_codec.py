from nobox import message


def test_outgoing_meta_roundtrip():
    meta = message.new_outgoing_meta(
        subject="Re: daily",
        in_reply_to=12345,
        pgp=False,
        flags=["\\Seen"],
    )
    body = message.build_comment_body("hello world", meta)
    parsed = message.extract_meta(body)
    assert parsed is not None
    assert parsed["v"] == 1
    assert parsed["direction"] == "out"
    assert parsed["subject"] == "Re: daily"
    assert parsed["in_reply_to"] == 12345
    assert parsed["pgp"] is False
    assert parsed["flags"] == ["\\Seen"]
    assert parsed["id"] == meta.id


def test_extract_meta_handles_no_block():
    assert message.extract_meta("just a user reply with no metadata") is None
    assert message.extract_meta("") is None


def test_strip_meta_removes_block_only():
    meta = message.new_outgoing_meta(
        subject="Re: x", in_reply_to=None, pgp=False
    )
    body = message.build_comment_body("body content", meta)
    stripped = message.strip_meta(body)
    assert "body content" in stripped
    assert "nobox-meta" not in stripped


def test_inbox_body_marker_roundtrip():
    body = message.build_inbox_body(
        user_login="evilsocket",
        name="daily",
        pgp_pub_armored=None,
        uid_validity=1234567890,
    )
    assert "@evilsocket" in body
    # logo banner is embedded in a fenced code block
    assert "```text" in body
    assert "NOBOX" not in body  # the logo uses box-drawing, not letters
    assert "███" in body
    # version tag appears (test runs against the installed version)
    from nobox import __version__
    assert __version__ in body
    # how-to-use lines
    assert "You → agent" in body
    assert "Agent → you" in body
    marker = message.parse_inbox_marker(body)
    assert marker is not None
    assert marker["name"] == "daily"
    assert marker["pgp"] == "false"
    assert marker["uid_validity"] == "1234567890"


def test_inbox_body_with_pgp_block():
    pubkey = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----"
    body = message.build_inbox_body(
        user_login="bob",
        name="secret",
        pgp_pub_armored=pubkey,
        uid_validity=42,
    )
    assert "BEGIN PGP PUBLIC KEY BLOCK" in body
    marker = message.parse_inbox_marker(body)
    assert marker["pgp"] == "true"
