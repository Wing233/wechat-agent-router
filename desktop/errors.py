"""Shared API error type for the desktop backend."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = int(status)
        self.message = str(message)
        super().__init__(self.message)
