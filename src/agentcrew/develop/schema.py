"""Closed plan/implementation contracts and their local trust-boundary rules."""

import re

from agentcrew.config import ROLE_ACCESS
from agentcrew.review.validate import bounded_result, bounded_text, invalid, validate_relative_path


def closed_object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
VERIFICATION = closed_object({"kind": {"type": "string", "enum": ["test", "manual"]},
                              "check": STRING, "test_id": STRING})
REQUIREMENT = closed_object({"id": STRING, "description": STRING, "verification": VERIFICATION})
WORK_ITEM = closed_object({"id": STRING, "role": STRING, "description": STRING, "depends_on": STRINGS})
PLAN_SCHEMA = closed_object({
    "schema_version": {"type": "integer", "enum": [1]}, "summary": STRING,
    "requirements": {"type": "array", "items": REQUIREMENT},
    "work_items": {"type": "array", "items": WORK_ITEM}, "constraints": STRINGS, "questions": STRINGS,
})
IMPLEMENTATION_SCHEMA = closed_object({
    "schema_version": {"type": "integer", "enum": [1]}, "summary": STRING, "changed_files": STRINGS,
    "addressed_requirements": STRINGS, "remaining_requirements": STRINGS, "limitations": STRINGS,
})


def _ids(values, prefix):
    if len(values) > 50 or len(set(values)) != len(values):
        invalid("Too many or duplicate IDs")
    if any(not re.fullmatch(prefix + r"[1-9][0-9]*", value) for value in values):
        invalid(f"IDs must use {prefix}1..{prefix}n")
    return set(values)


def validate_plan(data: dict, checks: list[dict]) -> dict:
    bounded_result(data, PLAN_SCHEMA)
    bounded_text(data["summary"], "summary", 2000)
    _ids([requirement["id"] for requirement in data["requirements"]], "A")
    work_ids = _ids([work["id"] for work in data["work_items"]], "W")
    check_names = {check["name"] for check in checks}
    for requirement in data["requirements"]:
        bounded_text(requirement["description"], "description", 2000, nonempty=True)
        verification = requirement["verification"]
        if verification["kind"] == "manual":
            if verification["check"] or verification["test_id"]:
                invalid("Manual verification must leave check and test_id empty")
        else:
            if verification["check"] not in check_names or not verification["test_id"].strip():
                invalid("Test verification needs a configured check and nonempty test ID")
            if "\x00" in verification["test_id"] or "\n" in verification["test_id"]:
                invalid("Invalid test ID")
            validate_relative_path(verification["test_id"].split("::", 1)[0])
    edges = {}
    for work in data["work_items"]:
        bounded_text(work["description"], "description", 2000, nonempty=True)
        if work["role"] not in ROLE_ACCESS:
            invalid("Unknown work-item role")
        dependencies = work["depends_on"]
        if len(set(dependencies)) != len(dependencies) or any(item not in work_ids for item in dependencies):
            invalid("Unknown or duplicate dependency")
        edges[work["id"]] = dependencies
    active, done = set(), set()

    def visit(work_id):
        if work_id in active:
            invalid("Work-item dependencies contain a cycle")
        if work_id not in done:
            active.add(work_id)
            for dependency in edges[work_id]:
                visit(dependency)
            active.remove(work_id)
            done.add(work_id)

    for work_id in work_ids:
        visit(work_id)
    return data


def validate_implementation(data: dict, plan: dict | None = None) -> dict:
    bounded_result(data, IMPLEMENTATION_SCHEMA)
    bounded_text(data["summary"], "summary", 2000)
    if len(set(data["changed_files"])) != len(data["changed_files"]):
        invalid("Duplicate changed file")
    for path in data["changed_files"]:
        validate_relative_path(path)
    addressed = _ids(data["addressed_requirements"], "A")
    remaining = _ids(data["remaining_requirements"], "A")
    if addressed & remaining:
        invalid("Addressed and remaining requirements overlap")
    if plan is not None:
        expected = {requirement["id"] for requirement in plan["requirements"]}
        if addressed | remaining != expected:
            invalid("Implementation requirements do not match the plan")
    return data
