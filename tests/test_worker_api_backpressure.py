import inspect
import unittest

from fastapi import HTTPException

from backend.app.routers import api_worker


class WorkerApiBackpressureTests(unittest.TestCase):
    def test_admission_returns_retryable_429_without_waiting(self) -> None:
        admission = api_worker.WorkerApiAdmission(max_inflight=1)
        admission.acquire_or_raise()

        with self.assertRaises(HTTPException) as context:
            admission.acquire_or_raise()

        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers, {"Retry-After": "3"})
        admission.release()

    def test_heavy_worker_endpoints_run_off_the_asgi_event_loop(self) -> None:
        for endpoint in (
            api_worker.register_worker,
            api_worker.heartbeat_worker,
            api_worker.claim_worker_job,
            api_worker.register_live_worker,
            api_worker.heartbeat_live_worker,
            api_worker.claim_live_worker_stream,
            api_worker.update_worker_job_progress,
            api_worker.update_live_stream_progress,
            api_worker.complete_worker_job,
            api_worker.fail_worker_job,
            api_worker.complete_live_stream,
            api_worker.fail_live_stream,
        ):
            self.assertFalse(inspect.iscoroutinefunction(endpoint), endpoint.__name__)


if __name__ == "__main__":
    unittest.main()
