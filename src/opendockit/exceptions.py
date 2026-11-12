"""Conversion failure exceptions. Each message is fixed English wording that can be shown to the user as is."""


class ConversionError(Exception):
    """Base of every conversion failure. At the server boundary, catching this one exception is enough."""

    code = "conversion_failed"


class UnsupportedFormatError(ConversionError):
    code = "unsupported_format"


class EncryptedDocumentError(ConversionError):
    code = "encrypted_document"


class DocumentTooLargeError(ConversionError):
    code = "document_too_large"


class CorruptedDocumentError(ConversionError):
    code = "corrupted_document"
