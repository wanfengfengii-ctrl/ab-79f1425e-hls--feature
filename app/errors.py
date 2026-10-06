"""Stable, machine-readable error codes surfaced by the API."""
from __future__ import annotations


class ApiError(Exception):
    """An error that maps to a stable error code in the HTTP response.

    ``segment`` carries the sequence number of the offending subtitle
    segment when the failure can be attributed to one.
    """

    def __init__(self, code: str, message: str, segment: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.segment = segment

    def with_segment(self, segment: int) -> "ApiError":
        if self.segment is None:
            self.segment = segment
        return self
