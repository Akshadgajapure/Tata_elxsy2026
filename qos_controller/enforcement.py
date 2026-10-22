"""Enforcement interfaces and a safe dry-run backend.

The dry-run backend records the policy that would be applied. It never invokes
the shell, which makes it suitable for local development and unit tests.
"""

from dataclasses import dataclass

from .models import Policy
from .policy import PolicyEngine


class EnforcementError(RuntimeError):
    """Raised when a policy cannot be validated or applied."""


class EnforcementBackend:
    def apply(self, policy: Policy) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError


@dataclass
class DryRunBackend(EnforcementBackend):
    policy_engine: PolicyEngine
    active_policy: Policy | None = None
    previous_policy: Policy | None = None
    fail_next_apply: bool = False
    _rollback_target: Policy | None = None

    def apply(self, policy: Policy) -> None:
        # Record the last known-good policy BEFORE validating, so a validation
        # failure cannot leave a stale rollback target (regression-tested).
        self._rollback_target = self.active_policy
        self.policy_engine.validate(policy)
        if self.fail_next_apply:
            self.fail_next_apply = False
            raise EnforcementError("simulated enforcement failure")
        self.previous_policy = self.active_policy
        self.active_policy = policy

    def rollback(self) -> None:
        self.active_policy = self._rollback_target
        self.previous_policy = None

    @property
    def rollback_target(self) -> Policy | None:
        """Read-only view of the last-known-good policy rollback() would restore."""
        return self._rollback_target
