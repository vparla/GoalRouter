# SPDX-License-Identifier: MIT
# File: src/goalrouter/async_tools.py
# Purpose: Shared cancellation-safe ownership helpers for asynchronous operations

"""Wait for owned asynchronous operations without abandoning their cleanup."""

import asyncio


async def wait_for_owned_task[T](
    owned_task: asyncio.Task[T],
) -> tuple[asyncio.CancelledError, ...]:
    """Wait through repeated owner cancellation until an owned task is terminal."""

    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Operation must run in an owned asyncio task")
    observed_cancelling = owner.cancelling()
    cancellations: list[asyncio.CancelledError] = []
    while not owned_task.done():
        try:
            await asyncio.shield(owned_task)
        except asyncio.CancelledError as cancellation:
            current_cancelling = owner.cancelling()
            if current_cancelling == observed_cancelling:
                break
            coalesced = current_cancelling - observed_cancelling
            if coalesced > 1:
                cancellation.add_note(
                    f"{coalesced - 1} additional cancellation request(s) were coalesced"
                )
            cancellations.append(cancellation)
            observed_cancelling = current_cancelling
        except BaseException:
            break
    return tuple(cancellations)


def prepare_cancellation(
    cancellations: tuple[asyncio.CancelledError, ...],
    *,
    operation: str,
) -> asyncio.CancelledError:
    """Combine repeated cancellation requests while retaining their diagnostics."""

    primary = cancellations[0]
    for additional in cancellations[1:]:
        message = str(additional) or "without a message"
        primary.add_note(
            f"Additional cancellation while waiting for owned {operation}: {message}"
        )
        for note in getattr(additional, "__notes__", ()):
            primary.add_note(f"Additional cancellation detail: {note}")
    return primary
