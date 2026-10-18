"""Run legacy blocking I/O without blocking the application's event loop."""

import asyncio
from functools import partial
from typing import Callable, ParamSpec, TypeVar

import anyio

P = ParamSpec("P")
T = TypeVar("T")


async def run_sync(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Use AnyIO's bounded pool; finish in-flight work before propagating cancellation.

    A request's SQLAlchemy Session must not be closed/reused while its worker is
    still committing. Cancellation cannot stop a thread, so drain that worker
    before the caller unwinds its dependencies. Calls sharing a Session must
    still be awaited sequentially.
    """
    worker = asyncio.create_task(anyio.to_thread.run_sync(partial(func, *args, **kwargs)))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        # Retrieve failures to avoid an unhandled-task warning; cancellation wins.
        if not worker.cancelled():
            worker.exception()
        raise
