import pytest

pgpy = pytest.importorskip("pgpy")

from nobox import pgp  # noqa: E402


def test_keypair_generation_smoke():
    """Just confirm keygen completes and the armored output is non-empty.
    Full RSA-4096 keygen is slow; we generate a smaller key for tests."""
    # PGPy doesn't let us cheaply override the size from our helper, so this
    # test is gated as slow. Skip it unless PGP_SLOW_TESTS=1.
    import os
    if not os.environ.get("PGP_SLOW_TESTS"):
        pytest.skip("set PGP_SLOW_TESTS=1 to run slow keygen test")
    keys = pgp.generate_inbox_keypair(uid="nobox-test <test@nobox.local>")
    assert "BEGIN PGP PRIVATE KEY" in keys.priv_armored
    assert "BEGIN PGP PUBLIC KEY" in keys.pub_armored
    assert len(keys.fingerprint) == 40


def test_find_pgp_block():
    msg = (
        "preamble\n"
        "-----BEGIN PGP MESSAGE-----\n"
        "Version: BCPG v1\n"
        "\n"
        "abcdef\n"
        "-----END PGP MESSAGE-----\n"
        "trailing"
    )
    block = pgp.find_pgp_block(msg)
    assert block is not None
    assert block.startswith("-----BEGIN PGP MESSAGE-----")
    assert block.endswith("-----END PGP MESSAGE-----")
    assert pgp.find_pgp_block("no block here") is None
    assert pgp.find_pgp_block("") is None
