"""Structured, actionable errors (compendium Appendix B)."""

from typing import Any, Dict


class GovernanceError(Exception):
    http_status = 400

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class AuthenticationError(GovernanceError):
    http_status = 401


class AuthorizationError(GovernanceError):
    http_status = 403


class NotFound(GovernanceError):
    http_status = 404


class Conflict(GovernanceError):
    http_status = 409


class ValidationFailure(GovernanceError):
    http_status = 422
