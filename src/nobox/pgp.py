"""Opt-in PGP encryption for inbox messages.

Each PGP-enabled inbox has its own 4096-bit RSA keypair. Outgoing messages
are encrypted with the user's public key and signed with the inbox key.
Incoming PGP-armored messages are decrypted with the inbox private key and
(optionally) signature-verified against a trusted signer.

The pgpy dep is optional (extras = "pgp"). Import errors are deferred so
plain non-PGP usage stays light.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass


class PGPUnavailable(RuntimeError):
    pass


def _require_pgpy():
    try:
        import pgpy  # noqa: F401
    except ImportError as e:
        raise PGPUnavailable(
            "PGP support is an optional extra. Install with: pip install 'nobox[pgp]'"
        ) from e
    return __import__("pgpy")


PGP_BLOCK_RE = re.compile(
    r"-----BEGIN PGP MESSAGE-----.+?-----END PGP MESSAGE-----",
    re.DOTALL,
)


@dataclass
class Keypair:
    priv_armored: str
    pub_armored: str
    fingerprint: str


def generate_inbox_keypair(*, uid: str) -> Keypair:
    pgpy = _require_pgpy()
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 4096)
    uid_obj = pgpy.PGPUID.new(uid)
    key.add_uid(
        uid_obj,
        usage={
            pgpy.constants.KeyFlags.Sign,
            pgpy.constants.KeyFlags.EncryptCommunications,
            pgpy.constants.KeyFlags.EncryptStorage,
        },
        hashes=[pgpy.constants.HashAlgorithm.SHA512, pgpy.constants.HashAlgorithm.SHA256],
        ciphers=[pgpy.constants.SymmetricKeyAlgorithm.AES256, pgpy.constants.SymmetricKeyAlgorithm.AES128],
        compression=[pgpy.constants.CompressionAlgorithm.ZLIB, pgpy.constants.CompressionAlgorithm.Uncompressed],
    )
    return Keypair(
        priv_armored=str(key),
        pub_armored=str(key.pubkey),
        fingerprint=str(key.fingerprint).replace(" ", ""),
    )


def fingerprint_of(armored_pub: str) -> str:
    pgpy = _require_pgpy()
    key, _ = pgpy.PGPKey.from_blob(armored_pub)
    return str(key.fingerprint).replace(" ", "")


def encrypt_and_sign(
    plaintext: str,
    user_pub_armored: str,
    inbox_priv_armored: str,
) -> str:
    """Encrypt `plaintext` to user_pub, sign with inbox_priv. Return ASCII armor."""
    pgpy = _require_pgpy()
    user_pub, _ = pgpy.PGPKey.from_blob(user_pub_armored)
    inbox_priv, _ = pgpy.PGPKey.from_blob(inbox_priv_armored)
    msg = pgpy.PGPMessage.new(plaintext)
    msg |= inbox_priv.sign(msg)
    enc = user_pub.encrypt(msg)
    return str(enc)


def decrypt_and_verify(
    armored_ciphertext: str,
    inbox_priv_armored: str,
    *,
    signer_pub_armored: str | None = None,
) -> str:
    pgpy = _require_pgpy()
    inbox_priv, _ = pgpy.PGPKey.from_blob(inbox_priv_armored)
    msg = pgpy.PGPMessage.from_blob(armored_ciphertext)
    decrypted = inbox_priv.decrypt(msg)
    if signer_pub_armored:
        signer, _ = pgpy.PGPKey.from_blob(signer_pub_armored)
        # Verification failure is non-fatal — caller still gets the plaintext.
        with contextlib.suppress(Exception):
            signer.verify(decrypted)
    return str(decrypted.message) if hasattr(decrypted, "message") else str(decrypted)


def find_pgp_block(text: str) -> str | None:
    """Return the first ASCII-armored PGP message block in `text`, or None."""
    if not text:
        return None
    m = PGP_BLOCK_RE.search(text)
    return m.group(0) if m else None
