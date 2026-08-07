# SPDX-License-Identifier: MIT
# File: src/goalrouter/approvals.py
# Purpose: Bind explicit dispatch approval to immutable state and route inputs

"""Explicit, fingerprint-bound approval enforcement."""

import hashlib
import json
from datetime import datetime

from goalrouter.domain import ApprovalMode, ApprovalRecord, RouteDecision, RunState, WorkItem
from goalrouter.errors import ApprovalRequiredError


class ApprovalService:
    """Create and validate approvals without any implicit authorization fallback."""

    def approve(
        self,
        state: RunState,
        *,
        work_item_id: str,
        approved_by: str,
        approved_at: datetime,
    ) -> ApprovalRecord:
        """Record approval for the exact current item, route, and configuration."""

        if approved_at.tzinfo is None:
            raise ApprovalRequiredError("Approval time must be timezone-aware")
        if not approved_by.strip():
            raise ApprovalRequiredError("Approval identity must not be empty")
        _require_item_and_route(state, work_item_id)
        record = ApprovalRecord(
            run_id=state.run_id,
            work_item_id=work_item_id,
            approved_by=approved_by,
            approved_at=approved_at,
            configuration_digest=state.configuration_digest,
            fingerprint=_fingerprint(state, work_item_id),
        )
        state.approvals[work_item_id] = record
        return record

    def require_dispatchable(self, state: RunState, work_item_id: str) -> None:
        """Reject approval-required work unless its current fingerprint is approved."""

        _, route = _require_item_and_route(state, work_item_id)
        if route.approval is ApprovalMode.AUTOMATIC:
            return

        record = state.approvals.get(work_item_id)
        if record is None:
            raise ApprovalRequiredError(
                f"Work item {work_item_id} requires explicit approval"
            )
        if record.run_id != state.run_id:
            raise ApprovalRequiredError(
                f"Approval for work item {work_item_id} belongs to another run"
            )
        if record.configuration_digest != state.configuration_digest:
            raise ApprovalRequiredError(
                f"Approval for work item {work_item_id} has a stale configuration digest"
            )
        if record.fingerprint != _fingerprint(state, work_item_id):
            raise ApprovalRequiredError(
                f"Approval fingerprint for work item {work_item_id} no longer matches"
            )


def _fingerprint(state: RunState, work_item_id: str) -> str:
    item, route = _require_item_and_route(state, work_item_id)
    payload = {
        "configuration_digest": state.configuration_digest,
        "route": route.to_dict(),
        "work_item": item.to_dict(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_item_and_route(
    state: RunState,
    work_item_id: str,
) -> tuple[WorkItem, RouteDecision]:
    item = state.work_items.get(work_item_id)
    route = state.routes.get(work_item_id)
    if item is None or route is None:
        raise ApprovalRequiredError(f"Unknown routed work item {work_item_id}")
    return item, route
