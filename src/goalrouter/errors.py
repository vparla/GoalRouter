# SPDX-License-Identifier: MIT
# File: src/goalrouter/errors.py
# Purpose: Explicit GoalRouter failures and stable CLI exit codes

"""Typed GoalRouter failures with stable process exit codes."""


class GoalRouterError(Exception):
    """Base class for expected, actionable GoalRouter failures."""

    exit_code = 1


class ConfigurationError(GoalRouterError):
    """The routing configuration is invalid."""

    exit_code = 2


class UnknownTaskError(GoalRouterError):
    """A requested task identifier is not configured."""

    exit_code = 3


class ModelUnavailableError(GoalRouterError):
    """A configured concrete model is not available to the account."""

    exit_code = 4


class RepositoryError(GoalRouterError):
    """Repository evidence could not be read safely."""

    exit_code = 5


class PlannerOutputError(GoalRouterError):
    """Planner output is structurally or semantically invalid."""

    exit_code = 6


class ApprovalRequiredError(GoalRouterError):
    """A work item requires an explicit valid approval."""

    exit_code = 7


class DependencyBlockedError(GoalRouterError):
    """A work item cannot run because a dependency did not succeed."""

    exit_code = 8


class SdkError(GoalRouterError):
    """The Codex SDK failed to initialize or complete a turn."""

    exit_code = 9


class AuthenticationError(GoalRouterError):
    """The explicitly selected authentication mode failed."""

    exit_code = 10


class TurnTimeoutError(GoalRouterError):
    """A Codex turn exceeded its configured timeout."""

    exit_code = 11


class StateError(GoalRouterError):
    """Persisted run state is corrupt or unsupported."""

    exit_code = 12


class ResumeConfigurationChangedError(GoalRouterError):
    """Resume was refused because the routing configuration changed."""

    exit_code = 13


class ProjectBusyError(GoalRouterError):
    """Another process currently owns the project's write lease."""

    exit_code = 14


class RunBusyError(GoalRouterError):
    """Another process currently owns the run's mutation lease."""

    exit_code = 15
