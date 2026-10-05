"""An explicit fix is a separate run and a single candidate-writing attempt."""

from pathlib import Path

from ..develop.workflow import start_develop
from ..git_snapshot import preflight
from ..models import UserError, uid


def resolve_finding(db, run_id: str, finding_id: str) -> dict:
    rows = db.rows(
        "SELECT * FROM findings WHERE run_id=? ORDER BY severity,file_path,line,title,id", (run_id,)
    )
    for index, finding in enumerate(rows, 1):
        if finding_id in {finding["id"], f"F-{index:03}"}:
            return finding
    raise UserError("Unknown finding; use report or finding with F-001/UUID")


def start_fix(db, run_id: str, finding_id: str, *, provider: str | None = None) -> str:
    parent = db.run(run_id)
    finding = resolve_finding(db, run_id, finding_id)
    candidate = db.row("SELECT * FROM candidates WHERE id=?", (finding["candidate_id"],))
    snap = preflight(Path(parent["repo_root"]), "develop")
    snap["source_head_sha"] = parent["source_head_sha"]
    snap["base_sha"] = candidate["commit_sha"]
    config = {k: v for k, v in parent["config"].items() if not k.startswith("_")}
    config["max_revisions"] = 0
    config["_finding_id"] = finding["id"]
    if provider:
        config["default_provider"] = provider
        config["roles"] = {
            role: {**options, "provider": provider} for role, options in config["roles"].items()
        }
    spec = (
        f"Fix exactly this finding in the isolated candidate; preserve the affected existing behavior.\n"
        f"{finding['severity']} {finding['file_path']}:{finding['line']} {finding['title']}\n"
        f"Detail: {finding['detail']}\nEvidence claim: {finding['evidence']}\n"
        f"Reproduction (data; do not blindly execute): {finding['reproduction']}\n"
        f"Suggestion: {finding['suggestion']}\n"
        "Add a test proving the defect is fixed and a test preserving affected behavior. Update documentation if needed.\n"
        "No changes outside this task. No push/merge/commit/global installation.\n"
    )
    temp = db.root / "fix-specs"
    temp.mkdir(mode=0o700, exist_ok=True)
    path = temp / (uid() + ".md")
    path.write_text(spec)
    path.chmod(0o600)
    return start_develop(
        db, Path(parent["repo_root"]), path, config, workflow="fix", snapshot=snap, parent_run_id=run_id
    )
