"""Provider-facing closed review contract; bounds are enforced locally."""

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "summary", "findings", "limitations"],
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "summary": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["title", "severity", "file", "line", "detail", "evidence",
                             "reproduction", "suggestion", "confidence"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "file": {"type": "string"}, "line": {"type": "integer"},
                    "detail": {"type": "string"}, "evidence": {"type": "string"},
                    "reproduction": {"type": "string"}, "suggestion": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}
