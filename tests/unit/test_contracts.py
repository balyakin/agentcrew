from copy import deepcopy

import pytest

from agentcrew.models import UserError, validate_schema


def review():
    return {"schema_version": 1, "summary": "Checked authentication", "findings": [{
        "title": "Empty token succeeds", "severity": "P1", "file": "src/auth.py", "line": 2,
        "detail": "The empty token enters the successful authentication branch.",
        "evidence": "token='' returns True", "reproduction": "Call authenticate('')",
        "suggestion": "Reject empty tokens", "confidence": 0.86}], "limitations": []}


@pytest.fixture
def candidate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/auth.py").write_text("def authenticate(token):\n    return True\n")
    return tmp_path


def test_review_validates_real_file_line_and_closed_schema(candidate):
    from agentcrew.review.schema import REVIEW_SCHEMA
    from agentcrew.review.validate import validate_review

    data = review()
    assert validate_review(data, candidate, {"src/auth.py": {2}}) == data
    validate_schema(data, REVIEW_SCHEMA)
    data["findings"][0]["tests_passed"] = True
    with pytest.raises(UserError) as err:
        validate_review(data, candidate)
    assert err.value.code == "provider_output_invalid"


@pytest.mark.parametrize("field,value", [
    ("title", ""), ("file", ""), ("detail", " "), ("evidence", ""), ("reproduction", ""),
    ("severity", "P3"), ("line", True), ("line", 0), ("line", 3),
    ("confidence", -0.1), ("confidence", 1.01), ("confidence", float("nan")),
    ("title", "x" * 501), ("evidence", "x" * 501), ("detail", "x" * 4001),
    ("reproduction", "x" * 2001), ("suggestion", "x" * 2001),
])
def test_invalid_finding_is_rejected(candidate, field, value):
    from agentcrew.review.validate import validate_review

    data = review()
    data["findings"][0][field] = value
    with pytest.raises(UserError) as err:
        validate_review(data, candidate)
    assert err.value.code == "provider_output_invalid"


def test_review_limits_accept_exact_boundaries_and_reject_overflow(candidate):
    from agentcrew.review.validate import validate_review

    data = review()
    data["summary"] = "x" * 2000
    data["limitations"] = ["x" * 500] * 20
    data["findings"] *= 20
    data["findings"][0].update(title="x" * 500, evidence="x" * 500,
                               detail="x" * 4000, reproduction="x" * 2000,
                               suggestion="x" * 2000, confidence=1)
    validate_review(data, candidate)
    for field, value in [("summary", "x" * 2001), ("limitations", ["x"] * 21),
                         ("limitations", ["x" * 501]), ("findings", [review()["findings"][0]] * 21)]:
        bad = deepcopy(data)
        bad[field] = value
        with pytest.raises(UserError):
            validate_review(bad, candidate)


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "src/../src/auth.py",
                                   "src\\auth.py", "src/auth.py\x00", "C:/Windows/file",
                                   "missing.py", "src"])
def test_review_paths_cannot_escape_or_reference_nonfiles(candidate, path):
    from agentcrew.review.validate import file_in_candidate

    with pytest.raises(UserError) as err:
        file_in_candidate(candidate, path)
    assert err.value.code == "provider_output_invalid"


def test_resolved_paths_block_symlink_escape_and_oversized_files(candidate):
    from agentcrew.review.validate import file_in_candidate

    outside = candidate.parent / "outside.py"
    outside.write_text("outside\n")
    (candidate / "escape.py").symlink_to(outside)
    with pytest.raises(UserError):
        file_in_candidate(candidate, "escape.py")
    (candidate / "alias.py").symlink_to(candidate / "src/auth.py")
    assert file_in_candidate(candidate, "alias.py") == candidate / "src/auth.py"
    with (candidate / "large.py").open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    with pytest.raises(UserError):
        file_in_candidate(candidate, "large.py")


def test_unchanged_lines_require_diff_relationship(candidate):
    from agentcrew.review.validate import validate_review

    data = review()
    with pytest.raises(UserError):
        validate_review(data, candidate, {"src/auth.py": {1}})
    data["findings"][0]["detail"] = (
        "The changed caller now supplies an empty token, triggering this unchanged branch.")
    assert validate_review(data, candidate, {"src/auth.py": {1}}) == data
    data["findings"][0]["detail"] = "Изменение вызова передаёт пустой токен в эту прежнюю ветку."
    validate_review(data, candidate, {"src/auth.py": {1}})


def test_unicode_separators_do_not_create_source_lines(candidate):
    from agentcrew.review.validate import validate_review

    (candidate / "src/auth.py").write_text("first\u2028second\n")
    with pytest.raises(UserError):
        validate_review(review(), candidate)


def test_payload_size_is_bounded_even_with_valid_individual_strings():
    from agentcrew.develop.schema import validate_plan

    data = plan()
    data["constraints"] = ["😀" * 2000] * 200
    with pytest.raises(UserError) as err:
        validate_plan(data, CHECKS)
    assert err.value.code == "provider_output_invalid"


def plan():
    return {"schema_version": 1, "summary": "Export orders", "requirements": [{
        "id": "A1", "description": "Empty orders produces a header", "verification": {
            "kind": "test", "check": "unit", "test_id": "tests/test_export.py::test_empty"}}],
        "work_items": [{"id": "W1", "role": "implementer", "description": "Implement export",
                        "depends_on": []}], "constraints": [], "questions": []}


CHECKS = [{"name": "unit", "argv": ["python", "-m", "pytest"], "timeout_seconds": 180,
           "result_format": "junit", "result_file": ".agentcrew-results/unit.xml"}]


def test_plan_accepts_configured_tests_and_manual_requirements():
    from agentcrew.develop.schema import PLAN_SCHEMA, validate_plan

    data = plan()
    data["requirements"].append({"id": "A2", "description": "Observe exported document",
                                 "verification": {"kind": "manual", "check": "", "test_id": ""}})
    assert validate_plan(data, CHECKS) == data
    validate_schema(data, PLAN_SCHEMA)


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(outcome="accepted"),
    lambda d: d["requirements"].append(deepcopy(d["requirements"][0])),
    lambda d: d["requirements"][0].update(id="invented", description=""),
    lambda d: d["requirements"][0].update(description="x" * 2001),
    lambda d: d["requirements"][0]["verification"].update(check="missing"),
    lambda d: d["requirements"][0]["verification"].update(test_id=""),
    lambda d: d["requirements"][0]["verification"].update(kind="manual"),
    lambda d: d["requirements"][0]["verification"].update(test_id="../test.py::case"),
    lambda d: d["work_items"][0].update(role="superuser"),
    lambda d: d["work_items"][0].update(depends_on=["W2"]),
    lambda d: d["work_items"][0].update(depends_on=["W1"]),
    lambda d: d["work_items"].append(deepcopy(d["work_items"][0])),
    lambda d: d.update(requirements=[{"id": f"A{i}", "description": "Requirement",
                                     "verification": {"kind": "manual", "check": "", "test_id": ""}}
                                    for i in range(1, 52)]),
    lambda d: d.update(work_items=[{"id": f"W{i}", "role": "implementer", "description": "Work",
                                   "depends_on": []} for i in range(1, 52)]),
])
def test_invalid_plans_are_rejected(mutation):
    from agentcrew.develop.schema import validate_plan

    data = plan()
    mutation(data)
    with pytest.raises(UserError) as err:
        validate_plan(data, CHECKS)
    assert err.value.code == "provider_output_invalid"


def test_plan_rejects_multistep_cycle_but_accepts_dependency_order():
    from agentcrew.develop.schema import validate_plan

    data = plan()
    data["work_items"][0]["depends_on"] = ["W2"]
    data["work_items"].append({"id": "W2", "role": "planner", "description": "Inspect API",
                               "depends_on": []})
    validate_plan(data, CHECKS)
    data["work_items"][1]["depends_on"] = ["W1"]
    with pytest.raises(UserError):
        validate_plan(data, CHECKS)


def implementation():
    return {"schema_version": 1, "summary": "Export implemented", "changed_files": ["src/export.py"],
            "addressed_requirements": ["A1"], "remaining_requirements": [], "limitations": []}


def test_implementation_claims_are_partitioned_against_plan_without_trusted_fields():
    from agentcrew.develop.schema import IMPLEMENTATION_SCHEMA, validate_implementation

    data = implementation()
    assert validate_implementation(data, plan()) == data
    validate_schema(data, IMPLEMENTATION_SCHEMA)
    data["outcome"] = "accepted"
    with pytest.raises(UserError):
        validate_implementation(data, plan())


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(summary="x" * 2001),
    lambda d: d.update(changed_files=["../source.py"]),
    lambda d: d.update(changed_files=["."]),
    lambda d: d.update(changed_files=["src/export.py"] * 201),
    lambda d: d.update(changed_files=["x" * 4097]),
    lambda d: d.update(addressed_requirements=["A2"]),
    lambda d: d.update(addressed_requirements=[]),
    lambda d: d.update(addressed_requirements=["A1", "A1"]),
    lambda d: d.update(remaining_requirements=["A1"]),
])
def test_invalid_implementation_claims_are_rejected(mutation):
    from agentcrew.develop.schema import validate_implementation

    data = implementation()
    mutation(data)
    with pytest.raises(UserError) as err:
        validate_implementation(data, plan())
    assert err.value.code == "provider_output_invalid"
