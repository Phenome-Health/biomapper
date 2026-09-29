"""Typed exception hierarchy for biomapper."""

from __future__ import annotations


class BioMapperError(Exception):
    """Base exception for all biomapper errors."""


class BioMapperAuthError(BioMapperError):
    """Raised on HTTP 401/403: the deployment requires a key and none was sent, or the key sent
    was rejected. The message says which."""


class BioMapperRateLimitError(BioMapperError):
    """Raised when the API signals rate limiting (HTTP 429).

    Attributes:
        retry_after: Suggested wait in seconds, if provided by the server.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BioMapperServerError(BioMapperError):
    """Raised for unrecoverable 5xx responses from the API."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class BioMapperTimeoutError(BioMapperError):
    """Raised when a request exceeds the configured timeout."""


class BioMapperConfigError(BioMapperError):
    """Raised for invalid client configuration, such as an explicit ``api_key`` together with
    ``anonymous=True``. A missing key is not an error: the client is keyless by default."""
