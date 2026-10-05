"""Conservative deduplication preserves different causes at the same location."""

from copy import deepcopy

from agentcrew.models import canonical, digest


def _normalized(value):
    return " ".join(value.casefold().split())


def aggregate(findings: list[dict]) -> list[dict]:
    groups = {}
    for original in findings:
        item = deepcopy(original)
        file = item.get("file", item.get("file_path"))
        identity = (file, item["line"], _normalized(item["title"]))
        # Case and whitespace inside code literals are part of a finding's exact cause.
        cause = (item["detail"], item["evidence"])
        key = (*identity, *cause)
        source = {key: item[key] for key in ("role", "origin_seat", "provider", "model", "candidate_sha")
                  if key in item}
        sources = item.get("sources", [source] if source else [])
        if key not in groups:
            item.update(file=file, fingerprint=digest(canonical(key)), sources=[],
                        possible_duplicate=[], status=item.get("status", "reported"))
            groups[key] = item
        result = groups[key]
        for source in sources:
            if source not in result["sources"]:
                result["sources"].append(source)
        result["severity"] = min(result["severity"], item["severity"])
    values = list(groups.values())
    for item in values:
        roles = {source.get("origin_seat", source.get("role")) for source in item["sources"]}
        roles.discard(None)
        if len(roles) > 1 and item["status"] == "reported":
            item["status"] = "corroborated"
    for item in values:
        item["possible_duplicate"] = [other["fingerprint"] for other in values if other is not item
                                      and (item["file"], item["line"]) == (other["file"], other["line"])]
    return sorted(values, key=lambda item: (item["severity"], item["file"], item["line"], item["title"]))
