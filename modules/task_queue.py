from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from queue import Queue, Empty
import threading
import time
import logging
from typing import Callable

logger = logging.getLogger(__name__)

@dataclass
class QueueTask:
    task_id: str
    user_id: int
    timeout_sec: int
    retries_left: int
    fn: Callable[[], str]
    on_success: Callable[[str], None]
    on_error: Callable[[str], None]


class TaskQueue:
    def __init__(self) -> None:
        self._queue: Queue[QueueTask] = Queue()
        self._active_ids: set[str] = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        logger.info("task_queue_worker_started")

    def submit(self, task: QueueTask) -> bool:
        with self._lock:
            if task.task_id in self._active_ids:
                logger.info("task_queue_reject_duplicate task_id=%s", task.task_id)
                return False
            self._active_ids.add(task.task_id)
        self._queue.put(task)
        logger.info("task_queue_accepted task_id=%s user_id=%s", task.task_id, task.user_id)
        return True

    def _finish(self, task_id: str) -> None:
        with self._lock:
            self._active_ids.discard(task_id)
        logger.info("task_queue_finished task_id=%s", task_id)

    def _loop(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=1)
            except Empty:
                continue

            try:
                logger.info("task_queue_run task_id=%s timeout=%s retries_left=%s", task.task_id, task.timeout_sec, task.retries_left)
                with ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(task.fn)
                    try:
                        result = future.result(timeout=task.timeout_sec)
                        task.on_success(result)
                        self._finish(task.task_id)
                    except TimeoutError:
                        if task.retries_left > 0:
                            task.retries_left -= 1
                            logger.warning("task_queue_timeout_retry task_id=%s retries_left=%s", task.task_id, task.retries_left)
                            self._queue.put(task)
                        else:
                            logger.error("task_queue_timeout_fail task_id=%s", task.task_id)
                            task.on_error("Timeout in task worker")
                            self._finish(task.task_id)
                    except Exception as e:
                        if task.retries_left > 0:
                            task.retries_left -= 1
                            time.sleep(1)
                            logger.warning("task_queue_error_retry task_id=%s retries_left=%s err=%s", task.task_id, task.retries_left, e)
                            self._queue.put(task)
                        else:
                            logger.exception("task_queue_error_fail task_id=%s", task.task_id)
                            task.on_error(str(e))
                            self._finish(task.task_id)
            finally:
                self._queue.task_done()
