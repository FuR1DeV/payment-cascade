from enum import StrEnum


class DomainError(Exception):
    """Base class for business rule violations."""


class InvalidStateTransition(DomainError):
    def __init__(self, entity: str, current: StrEnum, target: StrEnum) -> None:
        super().__init__(f"{entity}: transition {current.value} -> {target.value} is not allowed")
        self.entity = entity
        self.current = current
        self.target = target


class ConflictingProviderOutcome(DomainError):
    """Provider reports 'not charged' for an attempt it has already approved."""


class AttemptNotAllowed(DomainError):
    pass


class AttemptNotFound(DomainError):
    pass
