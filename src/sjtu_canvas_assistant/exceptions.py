"""Application-specific exceptions with token-safe messages."""


class AssistantError(Exception):
    """Base class for expected user-facing errors."""


class ConfigError(AssistantError):
    """Configuration is missing or invalid."""


class CanvasAPIError(AssistantError):
    """Canvas returned an unexpected HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class CanvasAuthError(CanvasAPIError):
    """Canvas rejected the configured token."""


class CanvasPermissionError(CanvasAPIError):
    """Canvas denied access to the requested resource."""


class CanvasNotFoundError(CanvasAPIError):
    """Canvas resource was not found."""


class CanvasNetworkError(AssistantError):
    """Network or timeout failure while talking to Canvas."""


class DownloadError(AssistantError):
    """A file download failed."""


class VideoIntegrationNotImplemented(AssistantError):
    """The SJTU video integration is intentionally a stub in v1."""
