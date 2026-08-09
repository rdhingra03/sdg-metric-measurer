"""Simple exception types shared by SDG pipeline infrastructure."""


class RetrievalError(RuntimeError):
    """An official source could not provide the required data."""


class SourceValidationError(RetrievalError):
    """A source responded, but its contents failed source-specific checks."""


class OutputError(RuntimeError):
    """A completed result could not be written safely."""

