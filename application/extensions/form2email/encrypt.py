import email as _email
import os
import subprocess
import tempfile
import typing as t
import base64
import logging
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


from flask_mail import Message

try:
    import pgpy

    _HAS_PGP = True
except ImportError as exc:
    _HAS_PGP = False
    logging.warning("PGP encryption is not available. pgpy not installed")
    logging.exception(exc)
    if t.TYPE_CHECKING:
        import pgpy  # type: ignore[import-not-found]  # noqa: F811

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization.pkcs7 import (
        PKCS7EnvelopeBuilder,
    )

    _HAS_CRYPTOGRAPHY = True
except ImportError as exc:
    _HAS_CRYPTOGRAPHY = False
    logging.warning("S/MIME encryption is not available. cryptography not installed")
    logging.exception(exc)

    if t.TYPE_CHECKING:
        from cryptography import x509  # type: ignore[import-not-found]  # noqa: F811
        from cryptography.hazmat.primitives import serialization  # type: ignore[import-not-found]  # noqa: F811
        from cryptography.hazmat.primitives.serialization.pkcs7 import (  # type: ignore[import-not-found]  # noqa: F811
            PKCS7EnvelopeBuilder,
        )


class EncryptedMessage(Message):

    def __init__(self, *args, encryption_key: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._encryption_key = encryption_key.strip()

    @classmethod
    def factory(
        cls,
        *args,
        encryption_key: t.Optional[str] = None,
        inline: bool = False,
        **kwargs,
    ) -> Message:
        """Factory that returns the appropriate message class based on encryption_key.

        - PGP public key block  → :class:`PGPEncryptedMessage`
        - X.509 certificate     → :class:`SMIMEEncryptedMessage`
        - No key                → plain :class:`~flask_mail.Message`
        """
        if not encryption_key:
            return Message(*args, **kwargs)

        key = encryption_key.strip()
        if key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            if inline:
                return PGPInlineEncryptedMessage(
                    *args, encryption_key=encryption_key, **kwargs
                )
            return PGPEncryptedMessage(*args, encryption_key=key, **kwargs)
        if key.startswith("-----BEGIN CERTIFICATE-----"):
            return SMIMEEncryptedMessage(*args, encryption_key=key, **kwargs)

        raise ValueError(
            "Cannot detect encryption type from key format. "
            "Expected a PGP public key block or X.509 certificate in PEM format."
        )

    @classmethod
    def copy_transport_headers(cls, src: MIMEBase, dst: MIMEBase) -> None:
        for header in ("Subject", "From", "To", "Date", "Message-ID", "Cc", "Reply-To"):
            value = src.get(header)
            if value:
                dst[header] = value


class PGPEncryptedMessage(EncryptedMessage):
    """A Message subclass that encrypts the full MIME payload using PGP/MIME (RFC 3156).

    Requires ``pgpy``: ``pip install pgpy``

    :param encryption_key: Recipient's PGP public key block (PEM/ASCII-armored format).
    """

    def _message(self) -> MIMEBase:
        if not _HAS_PGP:
            raise ValueError("pgpy is required for PGP encryption")

        inner = super()._message()
        inner_bytes = inner.as_bytes()

        key, _ = pgpy.PGPKey.from_blob(self._encryption_key)  # type: ignore
        pgp_msg = pgpy.PGPMessage.new(inner_bytes, file=True)
        encrypted_payload = str(key.encrypt(pgp_msg))

        outer = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")

        version_part = MIMEBase("application", "pgp-encrypted")
        version_part["Content-Description"] = "PGP/MIME version identification"
        version_part.set_payload("Version: 1\n")

        data_part = MIMEBase("application", "octet-stream")
        data_part["Content-Description"] = "OpenPGP encrypted message"
        data_part.set_payload(encrypted_payload)

        outer.attach(version_part)
        outer.attach(data_part)
        self.copy_transport_headers(inner, outer)
        return outer


class PGPInlineEncryptedMessage(EncryptedMessage):
    """A Message subclass that encrypts the body using PGP and presents the
    ASCII-armored ciphertext as a plain-text section of the email, without
    wrapping the full MIME structure (no PGP/MIME multipart).

    Requires ``pgpy``: ``pip install pgpy``

    :param encryption_key: Recipient's PGP public key block (PEM/ASCII-armored format).
    """

    def _message(self) -> MIMEBase:
        if not _HAS_PGP:
            raise ValueError("pgpy is required for PGP encryption")

        body = self.body or self.html or ""
        key, _ = pgpy.PGPKey.from_blob(self._encryption_key)  # type: ignore
        pgp_msg = pgpy.PGPMessage.new(body)
        encrypted_payload = str(key.encrypt(pgp_msg))

        inner = super()._message()
        text_part = MIMEText(encrypted_payload, "plain", "utf-8")
        self.copy_transport_headers(inner, text_part)
        return text_part


class SMIMEEncryptedMessage(EncryptedMessage):
    """A Message subclass that encrypts the full MIME payload using S/MIME (RFC 5751).

    Requires ``cryptography``: ``pip install cryptography``

    :param encryption_key: Recipient's X.509 certificate (PEM format).
    """

    def _message(self) -> MIMEBase:
        if not _HAS_CRYPTOGRAPHY:
            raise ValueError("cryptography is required for S/MIME encryption")

        inner = super()._message()
        inner_bytes = inner.as_bytes()

        cert = x509.load_pem_x509_certificate(self._encryption_key.encode())
        encrypted_bytes = (
            PKCS7EnvelopeBuilder()
            .set_data(inner_bytes)
            .add_recipient(cert)
            .encrypt(serialization.Encoding.DER, [])
        )

        outer = MIMEBase("application", "pkcs7-mime")
        outer.set_param("smime-type", "enveloped-data")
        outer.set_param("name", "smime.p7m")
        outer.add_header("Content-Disposition", "attachment", filename="smime.p7m")
        outer.add_header("Content-Transfer-Encoding", "base64")
        outer.set_payload(base64.encodebytes(encrypted_bytes).decode())

        self.copy_transport_headers(inner, outer)
        return outer


# ---------------------------------------------------------------------------
# Decryption helpers
# ---------------------------------------------------------------------------


def detect_encryption_type(message_bytes: bytes) -> t.Optional[str]:
    """Detect the encryption type of a raw email message.

    Returns one of ``'pgp-mime'``, ``'pgp-inline'``, ``'smime'``, or ``None``
    if no recognised encryption is found.
    """
    msg = _email.message_from_bytes(message_bytes)
    ct = msg.get_content_type()

    if ct == "multipart/encrypted":
        if msg.get_param("protocol") == "application/pgp-encrypted":
            return "pgp-mime"

    elif ct == "application/pkcs7-mime":
        if msg.get_param("smime-type") == "enveloped-data":
            return "smime"

    elif ct == "text/plain":
        payload = msg.get_payload()
        if isinstance(payload, str) and "-----BEGIN PGP MESSAGE-----" in payload:
            return "pgp-inline"

    return None


def decrypt_pgp_message(
    message_bytes: bytes,
    private_key: str,
    passphrase: t.Optional[str] = None,
) -> bytes:
    """Decrypt a PGP/MIME or PGP-inline encrypted email message.

    Requires ``pgpy``: ``pip install pgpy``

    :param message_bytes: Raw bytes of the email message.
    :param private_key: ASCII-armored PGP private key.
    :param passphrase: Passphrase to unlock the private key, if protected.
    :returns: Decrypted content bytes (inner MIME message for PGP/MIME, or
              plain text for inline).
    :raises ValueError: If pgpy is unavailable or the message cannot be decrypted.
    """
    if not _HAS_PGP:
        raise ValueError("pgpy is required for PGP decryption")

    key, _ = pgpy.PGPKey.from_blob(private_key.strip())  # type: ignore

    msg = _email.message_from_bytes(message_bytes)

    if msg.get_content_type() == "multipart/encrypted":
        # PGP/MIME — encrypted payload is in the application/octet-stream part
        encrypted_payload = None
        for part in msg.walk():
            if part.get_content_type() == "application/octet-stream":
                encrypted_payload = part.get_payload()
                break

        if encrypted_payload is None:
            raise ValueError("No encrypted payload found in PGP/MIME message")

        pgp_msg = pgpy.PGPMessage.from_blob(encrypted_payload)  # type: ignore
    else:
        # PGP inline — body IS the armored ciphertext
        body = msg.get_payload()
        if not isinstance(body, str) or "-----BEGIN PGP MESSAGE-----" not in body:
            raise ValueError("No PGP encrypted content found in message")
        pgp_msg = pgpy.PGPMessage.from_blob(body)  # type: ignore

    if key.is_protected:
        if not passphrase:
            raise ValueError("Private key is passphrase-protected; supply --passphrase")
        with key.unlock(passphrase):
            decrypted = key.decrypt(pgp_msg)
    else:
        decrypted = key.decrypt(pgp_msg)

    result = decrypted.message
    return result if isinstance(result, bytes) else result.encode()


def decrypt_smime_message(
    message_bytes: bytes,
    key_path: str,
    cert_path: str,
    passphrase: t.Optional[str] = None,
) -> bytes:
    """Decrypt an S/MIME encrypted email message using the system ``openssl`` binary.

    :param message_bytes: Raw bytes of the email message.
    :param key_path: Path to the recipient's PEM private key file.
    :param cert_path: Path to the recipient's PEM certificate file.
    :param passphrase: Passphrase for the private key, if protected.
    :returns: Decrypted content bytes (the inner MIME message).
    :raises ValueError: If ``openssl`` fails or is unavailable.
    """
    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
        tmp.write(message_bytes)
        tmp_path = tmp.name

    try:
        cmd = [
            "openssl", "smime", "-decrypt",
            "-in", tmp_path,
            "-inkey", key_path,
            "-recip", cert_path,
        ]
        if passphrase:
            cmd.extend(["-passin", f"pass:{passphrase}"])

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise ValueError(
                f"S/MIME decryption failed: {result.stderr.decode().strip()}"
            )
        return result.stdout
    finally:
        os.unlink(tmp_path)
