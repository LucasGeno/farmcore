"""Run queued durable tasks in the test process, as `farmcore worker` would. Not a test module."""

from __future__ import annotations

from sqlalchemy import text

from farmcore.core import tasks


def drain(session_factory, settings, manifests) -> int:
    """Claim every task queued now and run it in this process, as a worker would; how many ran.
    Test-only: claims carry no worker, so a running worker would treat them as stalled."""
    specs = {spec.name: spec for spec in tasks.durable_specs(manifests)}
    fetch = text("SELECT id, task_name, args FROM procrastinate_fetch_job_v2(:q, NULL)")
    with session_factory() as session:
        claimed = []
        while (job := session.execute(fetch, {"q": [tasks.QUEUE]}).first()).id is not None:
            claimed.append(job)
        session.commit()
    for job in claimed:
        tasks.run_task(session_factory, settings, specs[job.task_name], job.args["farm_id"])
    with session_factory() as session:
        for job in claimed:
            session.execute(
                text("SELECT procrastinate_finish_job_v1(:id, 'succeeded', false)"), {"id": job.id}
            )
        session.commit()
    return len(claimed)
