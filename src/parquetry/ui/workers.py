"""Run blocking work (polars queries, exports) off the UI thread."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

_active: set[Task] = set()


class Task(QObject):
    """A unit of background work whose callbacks run on the UI thread.

    The Task object lives in the UI thread; its signals are emitted from the
    worker thread, so Qt queues the connected slots back to the UI thread.
    """

    done = Signal(object)
    failed = Signal(object)
    progress = Signal(object)

    def __init__(
        self,
        fn: Callable[..., Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
    ) -> None:
        super().__init__()
        self._fn = fn
        self._on_done = on_done
        self._on_error = on_error
        self._on_progress = on_progress
        self.done.connect(self._handle_done)
        self.failed.connect(self._handle_failed)
        self.progress.connect(self._handle_progress)

    @Slot(object)
    def _handle_done(self, result: Any) -> None:
        _active.discard(self)
        if self._on_done:
            self._on_done(result)

    @Slot(object)
    def _handle_failed(self, exc: BaseException) -> None:
        _active.discard(self)
        if self._on_error:
            self._on_error(exc)
        else:  # pragma: no cover - surfaced on stderr for debugging
            traceback.print_exception(exc)

    @Slot(object)
    def _handle_progress(self, value: Any) -> None:
        if self._on_progress:
            self._on_progress(value)


class _Runner(QRunnable):
    def __init__(self, task: Task, pass_progress: bool) -> None:
        super().__init__()
        self._task = task
        self._pass_progress = pass_progress

    def run(self) -> None:
        task = self._task
        try:
            if self._pass_progress:
                result = task._fn(task.progress.emit)
            else:
                result = task._fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the UI
            task.failed.emit(exc)
        else:
            task.done.emit(result)


def submit(
    fn: Callable[..., Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[Any], None] | None = None,
) -> Task:
    """Run ``fn()`` in the global thread pool.

    If *on_progress* is given, ``fn`` is called with a thread-safe
    ``report(value)`` callback whose values are delivered to *on_progress* on
    the UI thread.
    """
    task = Task(fn, on_done, on_error, on_progress)
    _active.add(task)
    QThreadPool.globalInstance().start(_Runner(task, on_progress is not None))
    return task


def wait_all(timeout_ms: int = 30_000) -> bool:
    """Block until all background work has finished (used on shutdown/tests)."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
