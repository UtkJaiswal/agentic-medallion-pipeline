"""Helpers for schema-constrained output: derive a provider-friendly JSON schema from a Pydantic model,
and robustly pull a JSON object out of model text (code fences, leading prose from small models)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

# Constraint keywords not uniformly supported by provider structured-output modes. They are stripped
# from the transport schema only; Pydantic still enforces them when validating the response.
_UNSUPPORTED = {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength", "pattern", "minItems", "maxItems", "format", "examples"}


def transport_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED and k != "properties"}
            if "properties" in node:  # property *names* are data, never strip them
                out["properties"] = {name: walk(sub) for name, sub in node["properties"].items()}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def extract_json(text: str) -> Any:
    """Parse the first complete JSON object/array in `text`."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return decoder.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")
