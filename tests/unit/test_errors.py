# SPDX-License-Identifier: MIT
# File: tests/unit/test_errors.py
# Purpose: Verify stable GoalRouter domain error exit codes

import pytest

from goalrouter.errors import (
    ApprovalRequiredError,
    AuthenticationError,
    ConfigurationError,
    DependencyBlockedError,
    GoalRouterError,
    ModelUnavailableError,
    PlannerOutputError,
    ProjectBusyError,
    RepositoryError,
    ResumeConfigurationChangedError,
    RunBusyError,
    SdkError,
    StateError,
    TurnTimeoutError,
    UnknownTaskError,
)


@pytest.mark.parametrize(
    ("error_type", "exit_code"),
    [
        (ConfigurationError, 2),
        (UnknownTaskError, 3),
        (ModelUnavailableError, 4),
        (RepositoryError, 5),
        (PlannerOutputError, 6),
        (ApprovalRequiredError, 7),
        (DependencyBlockedError, 8),
        (SdkError, 9),
        (AuthenticationError, 10),
        (TurnTimeoutError, 11),
        (StateError, 12),
        (ResumeConfigurationChangedError, 13),
        (ProjectBusyError, 14),
        (RunBusyError, 15),
    ],
)
def test_errors_have_stable_exit_codes(
    error_type: type[GoalRouterError], exit_code: int
) -> None:
    error = error_type("actionable context")

    assert error.exit_code == exit_code
    assert str(error) == "actionable context"
    assert isinstance(error, GoalRouterError)
