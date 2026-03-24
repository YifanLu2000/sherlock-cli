from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import JobMetadata, StateData


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> StateData:
        if not self.path.exists():
            return StateData()
        raw = json.loads(self.path.read_text())
        jobs = {
            job_id: JobMetadata(**payload)
            for job_id, payload in raw.get("jobs", {}).items()
        }
        return StateData(jobs=jobs)

    def save(self) -> None:
        payload = {"jobs": {job_id: asdict(metadata) for job_id, metadata in self._data.jobs.items()}}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    @property
    def jobs(self) -> dict[str, JobMetadata]:
        return self._data.jobs

    def managed_job_ids(self) -> set[str]:
        return set(self._data.jobs)

    def get(self, job_id: str) -> JobMetadata | None:
        return self._data.jobs.get(job_id)

    def upsert(self, metadata: JobMetadata) -> JobMetadata:
        self._data.jobs[metadata.job_id] = metadata
        self.save()
        return metadata

    def update_tunnel(
        self,
        job_id: str,
        pid: int | None,
        local_port: int | None,
        jupyter_url: str | None,
    ) -> None:
        metadata = self._data.jobs.get(job_id)
        if not metadata:
            return
        metadata.tunnel_pid = pid
        metadata.local_port = local_port
        metadata.jupyter_url = jupyter_url
        self.save()

    def clear_tunnel(self, job_id: str) -> None:
        self.update_tunnel(job_id, None, None, None)

    def record_submission(
        self,
        job_id: str,
        job_name: str,
        preset_id: str,
        notebook_dir: str,
        remote_port: int,
        remote_stdout: str,
        remote_stderr: str,
        remote_template: str,
    ) -> JobMetadata:
        metadata = JobMetadata(
            job_id=job_id,
            job_name=job_name,
            preset_id=preset_id,
            notebook_dir=notebook_dir,
            remote_port=remote_port,
            remote_stdout=remote_stdout,
            remote_stderr=remote_stderr,
            remote_template=remote_template,
            created_at=utc_now(),
        )
        return self.upsert(metadata)
