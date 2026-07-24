"""Configuration resolution errors with file/field context."""

from __future__ import annotations


class ConfigError(ValueError):
    """User-facing configuration error."""

    def __init__(
        self,
        message: str,
        *,
        file: str | None = None,
        field: str | None = None,
    ) -> None:
        parts = [message]
        if file is not None:
            parts.append(f"file={file}")
        if field is not None:
            parts.append(f"field={field}")
        super().__init__("; ".join(parts))
        self.file = file
        self.field = field
