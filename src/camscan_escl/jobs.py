"""In-memory job records. One job at a time; reap after five minutes (§4)."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .escl import ScanSettings

JOB_TTL_S = 300


@dataclass
class Job:
    id: str
    settings: ScanSettings
    created_at: float
    delivered: bool = False
    failed: bool = False


class JobStore:
    """Serialises scan jobs. A second live job is refused, not queued."""

    def __init__(self, ttl_s: float = JOB_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active: str | None = None
        self.scanning = False

    def _reap_locked(self, now: float) -> None:
        stale = [j.id for j in self._jobs.values() if now - j.created_at > self._ttl_s]
        for jid in stale:
            del self._jobs[jid]
            if self._active == jid:
                self._active = None

    def create(self, settings: ScanSettings, now: float | None = None) -> Job | None:
        """Return a new Job, or None if one is already in flight."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._reap_locked(now)
            if self._active is not None:
                return None
            job = Job(id=str(uuid.uuid4()), settings=settings, created_at=now)
            self._jobs[job.id] = job
            self._active = job.id
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._reap_locked(time.monotonic())
            return self._jobs.get(job_id)

    def finish(self, job_id: str, failed: bool = False) -> None:
        """Mark the page delivered (or the job failed) and free the slot."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.delivered = True
                job.failed = failed
            if self._active == job_id:
                self._active = None

    def delete(self, job_id: str) -> bool:
        with self._lock:
            existed = self._jobs.pop(job_id, None) is not None
            if self._active == job_id:
                self._active = None
            return existed
