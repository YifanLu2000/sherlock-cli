from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from .models import JobDetail, JobInfo, JupyterInfo


SBATCH_JOB_RE = re.compile(r"(?P<job_id>\d+)")
JUPYTER_URL_RE = re.compile(r"https?://[^\s'\"]+")


def parse_sbatch_submission(output: str) -> str:
    match = SBATCH_JOB_RE.search(output)
    if not match:
        raise ValueError(f"Could not parse job id from sbatch output: {output!r}")
    return match.group("job_id")


def parse_squeue_jobs(output: str, managed_job_ids: set[str]) -> list[JobInfo]:
    jobs: list[JobInfo] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 6:
            continue
        job_id, name, state, partition, reason_or_node, elapsed = [part.strip() for part in parts]
        if not job_id or not job_id[0].isdigit():
            continue
        jobs.append(
            JobInfo(
                job_id=job_id,
                name=name,
                state=state,
                partition=partition,
                reason_or_node=reason_or_node,
                elapsed=elapsed,
                origin="CLI-managed" if job_id in managed_job_ids else "external",
                connected=False,
            )
        )
    return jobs


def parse_sacct_job(output: str, managed_job_ids: set[str]) -> JobInfo | None:
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 6:
            continue
        job_id, name, state, partition, node_list, elapsed = [part.strip() for part in parts]
        if "." in job_id:
            continue
        return JobInfo(
            job_id=job_id,
            name=name,
            state=state,
            partition=partition,
            reason_or_node=node_list,
            elapsed=elapsed,
            origin="CLI-managed" if job_id in managed_job_ids else "external",
            connected=False,
        )
    return None


def parse_scontrol_kv(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for token in output.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        data[key] = value
    return data


def parse_job_detail(output: str) -> JobDetail:
    data = parse_scontrol_kv(output)
    return JobDetail(
        job_id=data.get("JobId", ""),
        name=data.get("JobName", ""),
        state=data.get("JobState", data.get("State", "")),
        partition=data.get("Partition", ""),
        node_list=data.get("NodeList", ""),
        reason=data.get("Reason", data.get("NodeList", "")),
        elapsed=data.get("RunTime", data.get("ElapsedTime", "")),
        stdout_path=data.get("StdOut"),
        stderr_path=data.get("StdErr"),
    )


def parse_jupyter_info(log_text: str) -> JupyterInfo | None:
    matches = JUPYTER_URL_RE.findall(log_text)
    if not matches:
        return None
    for candidate in matches:
        if "/lab" not in candidate and "/tree" not in candidate:
            continue
        parsed = urlsplit(candidate.rstrip("/"))
        token = None
        if "token=" in parsed.query:
            query_parts = dict(
                item.split("=", 1) for item in parsed.query.split("&") if "=" in item
            )
            token = query_parts.get("token")
        return JupyterInfo(url=candidate, port=parsed.port or 0, token=token)
    return None


def rewrite_local_jupyter_url(remote_url: str, local_port: int) -> str:
    parsed = urlsplit(remote_url)
    return urlunsplit(("http", f"localhost:{local_port}", parsed.path, parsed.query, parsed.fragment))
