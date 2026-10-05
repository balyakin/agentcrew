"""Validate reviewer claims against the frozen candidate, without executing text."""

import re
from pathlib import Path, PurePosixPath

from agentcrew.models import MAX_JSON, MAX_LOG, UserError, canonical, validate_schema
from agentcrew.review.schema import REVIEW_SCHEMA


def invalid(message):
    raise UserError(message, "provider_output_invalid")


def validate_relative_path(relative: str) -> Path:
    if type(relative) is not str or not relative.strip() or len(relative) > 4096:
        invalid("Invalid candidate-relative path")
    rel = PurePosixPath(relative)
    if (
        not rel.parts
        or "\x00" in relative
        or "\\" in relative
        or rel.is_absolute()
        or ".." in rel.parts
        or re.match(r"^[A-Za-z]:", relative)
    ):
        invalid("Path escapes the candidate")
    return Path(*rel.parts)


def file_in_candidate(root: Path, relative: str) -> Path:
    rel = validate_relative_path(relative)
    try:
        base = root.resolve(strict=True)
        target = (base / rel).resolve(strict=True)
        if not target.is_relative_to(base) or not target.is_file():
            invalid("Finding file is not inside the candidate")
        if target.stat().st_size > MAX_LOG:
            invalid("Finding file exceeds 20 MiB")
        return target
    except (OSError, ValueError, RuntimeError) as exc:
        raise UserError(f"Cannot read candidate file: {exc}", "provider_output_invalid") from exc


def bounded_result(data: dict, schema: dict):
    validate_schema(data, schema)
    if len(canonical(data).encode("utf-8")) > MAX_JSON:
        invalid("Provider result exceeds 1 MiB")


def bounded_text(text: str, label: str, maximum: int, *, nonempty=False):
    if len(text) > maximum or (nonempty and not text.strip()):
        invalid(f"{label}: empty or too long")


def validate_review(
    data: dict, root: Path, changed_lines: dict | None = None, *, candidate_sha: str | None = None
) -> dict:
    bounded_result(data, REVIEW_SCHEMA)
    bounded_text(data["summary"], "summary", 2000)
    if len(data["findings"]) > 20 or len(data["limitations"]) > 20:
        invalid("Review exceeds 20 findings or limitations")
    for limitation in data["limitations"]:
        bounded_text(limitation, "limitation", 500)
    for finding in data["findings"]:
        for field, maximum in {
            "title": 500,
            "evidence": 500,
            "detail": 4000,
            "reproduction": 2000,
            "suggestion": 2000,
        }.items():
            bounded_text(finding[field], field, maximum, nonempty=field != "suggestion")
        if not 0 <= finding["confidence"] <= 1:
            invalid("confidence must be in [0, 1]")
        path = file_in_candidate(root, finding["file"])
        if candidate_sha:
            from agentcrew.git_snapshot import git

            try:
                git(root, "cat-file", "-e", f"{candidate_sha}:{finding['file']}")
            except UserError as exc:
                raise UserError(
                    "Finding file is absent from the frozen commit", "provider_output_invalid"
                ) from exc
        try:
            content = path.read_bytes()
            if len(content) > MAX_LOG:
                invalid("Finding file exceeds 20 MiB")
            content.decode("utf-8")
            line_count = content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))
        except (OSError, UnicodeError) as exc:
            raise UserError(f"Cannot read finding text: {exc}", "provider_output_invalid") from exc
        if not 1 <= finding["line"] <= line_count:
            invalid("Finding line does not exist in candidate")
        # ponytail: textual link is a guard; semantic relevance still needs a reviewer.
        if (
            changed_lines is not None
            and finding["line"] not in changed_lines.get(finding["file"], ())
            and not re.search(
                r"\b(?:diff|changes?|changed|modified|added|removed|base)\b|"
                r"измен|добавлен|удален|удалён|разниц|базов",
                finding["detail"],
                re.IGNORECASE,
            )
        ):
            invalid("Finding outside changed lines must explain its connection to the diff")
    return data
