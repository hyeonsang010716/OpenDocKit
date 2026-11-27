"""PDFs encrypted with AES, written by reportlab.

reportlab encrypts with RC4 (its `strength` 40 and 128), and with AES only at 256 bits, only with the pyaes package and only
in revision 5, which PDF 2.0 deprecated. pdfminer decrypts RC4 with its own code and AES with the cryptography package, so
an RC4 document does not reach cryptography at all. `AesEncryption` keeps reportlab's writer and replaces the cipher:
AES-128 is revision 4 (`/CFM /AESV2`), AES-256 is revision 6 (`/CFM /AESV3`). The algorithm numbers below are those of
ISO 32000-2, 7.6.
"""

import hashlib
import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from reportlab.lib.pdfencrypt import StandardEncryption, hexText
from reportlab.pdfbase.pdfdoc import DummyDoc, PDFDictionary, PDFName, PDFObject


def _aes(key: bytes, data: bytes, iv: bytes | None) -> bytes:
    """`data` (whole blocks) encrypted with AES in CBC mode from `iv`, or in ECB mode when `iv` is None."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB() if iv is None else modes.CBC(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _hash(password: bytes, salt: bytes, user_key: bytes) -> bytes:
    """Algorithm 2.B: the hash of a revision 6 password."""
    k = hashlib.sha256(password + salt + user_key).digest()
    rounds = 0
    while True:
        rounds += 1
        e = _aes(k[:16], (password + k + user_key) * 64, k[16:32])
        k = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[sum(e[:16]) % 3](e).digest()
        if rounds >= 64 and e[-1] <= rounds - 32:
            return k[:32]


class AesEncryption(StandardEncryption):
    """Pass as `encrypt` to a reportlab canvas, like `StandardEncryption`: `AesEncryption(128, "", "owner", canCopy=0)`."""

    def __init__(self, bits: int, user_password: str, owner_password: str, **permissions: int) -> None:
        if bits not in (128, 256):
            raise ValueError("AES is 128 or 256 bits")
        super().__init__(user_password, owner_password, strength=128, **permissions)
        self.bits = bits

    def prepare(self, document: object, overrideID: bytes | None = None) -> None:
        # Revision 4 derives P, O, U and the file key as revision 3 does (algorithms 2, 3 and 5), which reportlab writes.
        super().prepare(document, overrideID)
        if self.bits == 256:
            user, owner = self.userPassword.encode()[:127], self.ownerPassword.encode()[:127]
            self.key = os.urandom(32)
            salts = os.urandom(16)  # validation salt, key salt
            self.U = _hash(user, salts[:8], b"") + salts  # algorithm 8
            self.UE = _aes(_hash(user, salts[8:], b""), self.key, bytes(16))
            salts = os.urandom(16)
            self.O = _hash(owner, salts[:8], self.U) + salts  # algorithm 9
            self.OE = _aes(_hash(owner, salts[8:], self.U), self.key, bytes(16))
            permissions = (self.P & 0xFFFFFFFF).to_bytes(4, "little") + b"\xff" * 4 + b"Tadb" + os.urandom(4)
            self.Perms = _aes(self.key, permissions, None)  # algorithm 10

    def encode(self, t: bytes | str) -> bytes:
        """A string or a stream of the object reportlab registered last (algorithm 1 with AES, or 1.A for AES-256)."""
        if self.bits == 256:
            key = self.key
        else:
            key = hashlib.md5(self.key + self.objnum.to_bytes(3, "little") + self.version.to_bytes(2, "little") + b"sAlT").digest()
        data = t.encode("utf-8") if isinstance(t, str) else t  # as reportlab's RC4 takes a str
        padder = padding.PKCS7(128).padder()
        iv = os.urandom(16)
        return iv + _aes(key, padder.update(data) + padder.finalize(), iv)

    def info(self) -> PDFObject:
        return _EncryptionDictionary(self)


class _EncryptionDictionary(PDFObject):
    __RefOnly__ = 1

    def __init__(self, encryption: AesEncryption) -> None:
        self.encryption = encryption

    def format(self, document: object) -> bytes:
        e = self.encryption
        entries = {"Filter": PDFName("Standard"), "O": hexText(e.O), "U": hexText(e.U), "P": e.P,
                   "StmF": PDFName("StdCF"), "StrF": PDFName("StdCF")}
        if e.bits == 256:
            entries.update(V=5, R=6, Length=256, OE=hexText(e.OE), UE=hexText(e.UE), Perms=hexText(e.Perms))
            method, length = "AESV3", 32
        else:
            entries.update(V=4, R=4, Length=128)
            method, length = "AESV2", 16
        filters = {"StdCF": PDFDictionary({"CFM": PDFName(method), "AuthEvent": PDFName("DocOpen"), "Length": length})}
        entries["CF"] = PDFDictionary(filters)
        return PDFDictionary(entries).format(DummyDoc())  # the dictionary itself is not encrypted
