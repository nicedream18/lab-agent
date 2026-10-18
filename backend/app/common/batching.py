"""Bounded FIFO micro-batches for synchronous inference callers in worker threads."""

from collections import deque
from concurrent.futures import Future
import logging
import threading
import time
from typing import Callable, Generic, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")
logger = logging.getLogger(__name__)


class BatchQueueFull(RuntimeError):
    pass


class MicroBatcher(Generic[T, R]):
    def __init__(self, predict: Callable[[list[T]], Sequence[R]], *, batch_size: int,
                 wait_ms: float, max_pending: int, name: str):
        if batch_size < 1 or max_pending < batch_size or wait_ms < 0:
            raise ValueError("invalid batch limits")
        self._predict = predict
        self.batch_size = batch_size
        self._wait_seconds = wait_ms / 1000
        self._max_pending = max_pending
        self._name = name
        self._condition = threading.Condition()
        self._queue: deque[tuple[T, Future[R]]] = deque()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._batches = self._items = self._errors = self._largest_batch = 0

    def map(self, items: Sequence[T]) -> list[R]:
        """Preserve order; large calls submit one bounded chunk at a time.

        Waiting here is intentional: Agent tools already run in a thread pool.
        Never call this directly on the event loop. No database Session is sent
        to the inference worker, only texts/pairs.
        """
        output: list[R] = []
        for start in range(0, len(items), self.batch_size):
            chunk = items[start:start + self.batch_size]
            futures: list[Future[R]] = [Future() for _ in chunk]
            with self._condition:
                if self._closed:
                    raise RuntimeError(f"{self._name} batcher closed")
                if len(self._queue) + len(chunk) > self._max_pending:
                    raise BatchQueueFull(f"{self._name} inference queue full")
                self._queue.extend(zip(chunk, futures))
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, daemon=True,
                                                    name=f"kb-{self._name}")
                    self._thread.start()
                self._condition.notify_all()
            try:
                output.extend(future.result() for future in futures)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        return output

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if self._closed and not self._queue:
                    return
                deadline = time.monotonic() + self._wait_seconds
                while len(self._queue) < self.batch_size and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                tasks = [self._queue.popleft()
                         for _ in range(min(self.batch_size, len(self._queue)))]
                tasks = [(item, future) for item, future in tasks
                         if future.set_running_or_notify_cancel()]
            if not tasks:
                continue
            started = time.perf_counter()
            error: Exception | None = None
            results: list[R] = []
            try:
                results = list(self._predict([item for item, _ in tasks]))
                if len(results) != len(tasks):
                    raise ValueError("inference result count does not match batch")
            except Exception as exc:
                error = exc
            with self._condition:
                self._batches += 1
                self._items += len(tasks)
                self._errors += int(error is not None)
                self._largest_batch = max(self._largest_batch, len(tasks))
            for index, (_, future) in enumerate(tasks):
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(results[index])
            logger.debug("%s inference batch=%d elapsed_ms=%.2f", self._name,
                         len(tasks), (time.perf_counter() - started) * 1000)

    def stats(self) -> dict[str, int]:
        with self._condition:
            return {"batches": self._batches, "items": self._items,
                    "errors": self._errors, "max_batch": self._largest_batch,
                    "pending": len(self._queue)}

    def close(self) -> None:
        """Reject queued work; wait for any active model call before shutdown."""
        with self._condition:
            self._closed = True
            while self._queue:
                _, future = self._queue.popleft()
                if future.set_running_or_notify_cancel():
                    future.set_exception(RuntimeError(f"{self._name} batcher closed"))
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()
