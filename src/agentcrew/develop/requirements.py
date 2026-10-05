"""Acceptance uses executed, candidate-bound proof rather than implementer claims."""

import json
from pathlib import Path

from agentcrew.models import MAX_LOG, UserError, canonical, digest
from agentcrew.review.checks import current_check_results


def development_outcome(*, required_checks: dict[str, str], requirements: dict[str, str],
                        reviewers: dict[str, str], unresolved_findings: int, isolation_ok: bool) -> str:
    if not isolation_ok:
        return "rejected"
    if not required_checks or not requirements:
        return "partial"
    if "failed" in required_checks.values() or "failed" in requirements.values() or unresolved_findings:
        return "rejected"
    if any(status != "passed" for status in required_checks.values()):
        return "partial"
    if any(status != "passed" for status in requirements.values()):
        return "partial"
    if any(reviewers.get(role) != "valid" for role in ("correctness", "tests")):
        return "partial"
    return "accepted"


def _proof(db, row, candidate):
    try:
        evidence = json.loads(row["result_json"])
        path = Path(evidence["result_path"])
        resolved = path.resolve(strict=True)
        worktree = db.row("SELECT path FROM worktrees WHERE candidate_id=? AND state='ready' AND ? LIKE path || '/%'",
                          (candidate["id"], str(resolved)))
        if (not worktree or not resolved.is_relative_to(Path(worktree["path"]).resolve())
                or resolved.stat().st_size > MAX_LOG or evidence["candidate_id"] != candidate["id"]
                or evidence["candidate_sha"] != candidate["commit_sha"]
                or digest(resolved.read_bytes()) != evidence["result_sha256"]):
            return None
        return evidence
    except (OSError, ValueError, TypeError, KeyError):
        return None


def evaluate_requirements(db, run_id: str, candidate_id: str, check_results: list[dict]) -> list[dict]:
    candidate = db.row("SELECT * FROM candidates WHERE id=? AND run_id=?", (candidate_id, run_id))
    if not candidate:
        raise UserError("Requirement evidence candidate does not belong to run")
    checks = []
    supplied_ids = {result.get("id", result.get("check_id")) for result in check_results}
    for row in current_check_results(db, run_id, candidate_id):
        if row["id"] in supplied_ids:
            proof = _proof(db, row, candidate)
            if proof is not None:
                checks.append((row, proof))
    results = []
    with db.transaction():
        for requirement in db.rows("SELECT * FROM requirements WHERE run_id=? ORDER BY label", (run_id,)):
            verification = json.loads(requirement["verification_json"])
            status, evidence = "unknown", {"reason": "No exact executed test or audited manual confirmation"}
            if verification["kind"] == "test":
                relevant = [(row, proof) for row, proof in checks if row["name"] == verification["check"]]
                if relevant:
                    row, proof = relevant[-1]
                    test_status = proof.get("tests", {}).get(verification["test_id"], "unknown")
                    if test_status == "failed":
                        status = "failed"
                    elif test_status == "passed" and row["state"] == "passed" and row["exit_code"] == 0:
                        status = "passed"
                    evidence = {"check_id": row["id"], "test_id": verification["test_id"],
                                "test_status": test_status, "exit_code": row["exit_code"],
                                "candidate_sha": candidate["commit_sha"], "result_path": proof["result_path"],
                                "result_sha256": proof["result_sha256"]}
            else:
                decision = db.row("SELECT * FROM decisions WHERE scope_id=? AND candidate_id=? AND kind='requirement' "
                                  "AND target IN (?,?) ORDER BY created_at DESC LIMIT 1",
                                  (run_id, candidate_id, requirement["id"], requirement["label"]))
                if decision:
                    try:
                        body = json.loads(decision["body"])
                        if body.get("status") in {"passed", "failed"} and body.get("actor") and body.get("evidence"):
                            status = body["status"]
                            evidence = {"decision_id": decision["id"], **body}
                    except (ValueError, TypeError, AttributeError):
                        pass
            db.execute("INSERT INTO requirement_results VALUES(?,?,?,?) ON CONFLICT(requirement_id,candidate_id) "
                       "DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json",
                       (requirement["id"], candidate_id, status, canonical(evidence)))
            results.append({"id": requirement["id"], "label": requirement["label"],
                            "description": requirement["description"], "status": status, "evidence": evidence})
        db.event(run_id, "requirements.evaluated", {"candidate_id": candidate_id,
                                                  "statuses": {r["label"]: r["status"] for r in results}})
    return results
