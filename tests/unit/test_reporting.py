from copy import deepcopy

import pytest


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "accepted"),
        ({"required_checks": {}}, "partial"),
        ({"requirements": {}}, "partial"),
        ({"requirements": {"A1": "unknown"}}, "partial"),
        ({"reviewers": {"correctness": "valid", "tests": "failed"}}, "partial"),
        ({"required_checks": {"unit": "failed"}}, "rejected"),
        ({"requirements": {"A1": "failed"}}, "rejected"),
        ({"unresolved_findings": 1}, "rejected"),
        ({"isolation_ok": False}, "rejected"),
    ],
)
def test_development_acceptance_requires_every_independent_condition(changes, expected):
    from agentcrew.develop.requirements import development_outcome

    args = {
        "required_checks": {"unit": "passed"},
        "requirements": {"A1": "passed"},
        "reviewers": {"correctness": "valid", "tests": "valid"},
        "unresolved_findings": 0,
        "isolation_ok": True,
        **changes,
    }
    assert development_outcome(**args) == expected


def finding(role="correctness"):
    return {
        "file": "src/auth.py",
        "line": 2,
        "title": "Empty token succeeds",
        "severity": "P1",
        "detail": "The caller passes an empty token",
        "evidence": "token='' returns True",
        "reproduction": "Call with empty token",
        "confidence": 0.8,
        "role": role,
    }


def test_aggregation_merges_identical_claims_without_turning_votes_into_reproduction():
    from agentcrew.review.aggregate import aggregate

    first, second = finding(), finding("tests")
    second["title"] = " empty TOKEN   succeeds "
    items = aggregate([first, second])
    assert len(items) == 1
    assert items[0]["status"] == "corroborated"
    assert {source["role"] for source in items[0]["sources"]} == {"correctness", "tests"}
    assert "fingerprint" in items[0]
    assert "sources" not in first


def test_same_location_different_causes_are_retained_and_sorted():
    from agentcrew.review.aggregate import aggregate

    first = finding()
    second = deepcopy(first)
    second.update(detail="The caller uses an expired token", evidence="expired token succeeds", severity="P0")
    items = aggregate([first, second])
    assert len(items) == 2
    assert items[0]["severity"] == "P0"
    assert all(item["possible_duplicate"] for item in items)
    assert items[0]["fingerprint"] != items[1]["fingerprint"]


def test_fingerprints_are_stable_when_reviewer_results_arrive_in_different_order():
    from agentcrew.review.aggregate import aggregate

    first = finding()
    second = deepcopy(first)
    second.update(detail="An expired token reaches this branch", evidence="Expired token succeeds")
    forward = {item["detail"]: item["fingerprint"] for item in aggregate([first, second])}
    backward = {item["detail"]: item["fingerprint"] for item in aggregate([second, first])}
    assert forward == backward


def test_report_failed_reviewer_is_partial_and_omits_logs(tmp_path):
    from agentcrew.db import Database
    from agentcrew.models import canonical, digest, now, uid
    from agentcrew.review.report import build_report, markdown

    db = Database(tmp_path / "state")
    run = db.create_scope("run", str(tmp_path))
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (run, "review", "a" * 40, "b" * 40, "report", canonical({"language": "en", "checks": []})),
    )
    candidate = db.add_candidate(run, "a" * 40, digest("patch"), None)
    for role, state, result in [
        (
            "correctness",
            "done",
            {
                "data": {"findings": [finding()], "limitations": []},
                "metadata": {"model": "explicit-model", "usage": {"input_tokens": 10}},
            },
        ),
        ("tests", "failed", {"error": "provider_output_invalid", "detail": "Invalid candidate line"}),
    ]:
        seat = db.add_seat(run, role, "codex", str(tmp_path))
        db.execute(
            "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,result_json,candidate_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                run,
                seat,
                "review",
                state,
                "{}",
                canonical(result) if result else None,
                candidate,
                now(),
                now(),
            ),
        )
    report = build_report(db, run)
    assert report["status"] == "partial"
    assert report["candidate_sha"] == "a" * 40
    assert report["agents"][1]["findings_count"] is None
    assert report["agents"][0]["findings_count"] == 1
    assert report["agents"][0]["model"] == "explicit-model"
    assert report["agents"][1]["error"] == "Invalid candidate line"
    assert report["cost_usd"] is None
    assert "logs" not in report
    text = markdown(report)
    russian = markdown(report, "ru")
    assert "## Агенты" in russian and "частично" in russian and "не проверено" in russian
    assert report["candidate_sha"] in russian and "source_head_sha" in russian
    assert "failed" in text
    assert "partial" in text
    assert "Invalid candidate line" in text
    assert "source_head_sha" in text
    db.close()


def test_report_preserves_planner_metadata_and_writer_files_after_freeze(tmp_path):
    from agentcrew.db import Database
    from agentcrew.models import canonical, digest, now, uid
    from agentcrew.review.report import build_report

    db = Database(tmp_path / "state")
    run = db.create_scope("run", str(tmp_path))
    config = {"checks": [], "_meta": {"command": "agentcrew develop --spec task.md", "started_at": now()}}
    db.execute(
        "INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) VALUES(?,?,?,?,?,?)",
        (run, "develop", "a" * 40, "a" * 40, "report", canonical(config)),
    )
    db.add_candidate(run, "b" * 40, digest("patch"), None)
    for role in ("planner", "implementer"):
        seat = db.add_seat(run, role, "codex", str(tmp_path))
        data = (
            {"summary": "Prepared", "changed_files": ["src/export.py"]}
            if role == "implementer"
            else {"summary": "Plan"}
        )
        payload = {
            "data": data,
            "metadata": {
                "model": "model",
                "usage": {"input_tokens": 3, "output_tokens": 1},
                "cost_usd": 0.01,
            },
        }
        db.execute(
            "INSERT INTO tasks(id,scope_id,owner_seat,kind,state,body_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                run,
                seat,
                "implement" if role == "implementer" else "plan",
                "done",
                "{}",
                canonical(payload),
                now(),
                now(),
            ),
        )
    report = build_report(db, run)
    assert {agent["model"] for agent in report["agents"]} == {"model"}
    assert report["changed_files"] == ["src/export.py"]
    assert report["command"] == "agentcrew develop --spec task.md"
    assert report["usage"] == {"input_tokens": 6, "output_tokens": 2}
    assert report["cost_usd"] == 0.02
    assert report["diff_reference"]["candidate_sha"] == "b" * 40
    db.close()
