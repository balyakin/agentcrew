import json

import pytest

from agentcrew.db import Database
from agentcrew.models import canonical, now, uid
from agentcrew.review.report import build_report


@pytest.mark.parametrize("lost_format_attempt", [False, True])
def test_usage_counts_prior_revisions_and_unknown_attempts(tmp_path, lost_format_attempt):
    db = Database(tmp_path / "state")
    run = db.create_scope("run", str(tmp_path))
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (run, "develop", "a" * 40, "a" * 40, "report", '{"checks": []}'),
    )
    seat = db.add_seat(run, "implementer", "claude", str(tmp_path))
    for index in (1, 2):
        task = uid()
        metadata = {"usage": {"input_tokens": index * 10}, "cost_usd": index / 10}
        db.execute(
            "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                task,
                run,
                seat,
                "implement" if index == 1 else "revise",
                "done",
                "{}",
                canonical({"data": {"summary": "Written"}, "metadata": metadata}),
                now(),
                now(),
            ),
        )
        db.execute(
            "INSERT INTO provider_results VALUES(?,?,?,?,?,?)",
            (task, 1, str(tmp_path / str(index)), json.dumps(metadata), 1, now()),
        )
    if lost_format_attempt:
        db.event(run, "provider_output_invalid", {"task_id": task, "format_attempt": 1})
    report = build_report(db, run)
    if lost_format_attempt:
        assert report["usage"] is None and report["cost_usd"] is None
    else:
        assert report["usage"] == {"input_tokens": 30}
        assert report["cost_usd"] == pytest.approx(0.3)
    db.close()
