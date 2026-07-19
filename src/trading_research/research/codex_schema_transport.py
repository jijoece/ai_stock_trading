"""Codex structured-output transport-schema normalizer.

`output_validation.py`'s canonical role/decision JSON Schemas are valid
Draft-07, but Codex's structured-output validator (the same strict JSON
Schema subset OpenAI's Structured Outputs enforces) rejects a narrower set
of constructs than Draft-07 allows — most concretely, numeric/string/array
bound keywords (`minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`,
`maximum`, ...) and any object node whose `required` list does not name
every key in `properties`. `anthropic_provider.py::_strict_compatible_schema`
already established this exact pattern for Claude's strict tool-use schema
compiler; this module is the Codex-specific analog, extended to fail closed
(rather than silently strip) on constructs it cannot safely represent.

This module only ever produces a *transport* schema — a broader constraint
used solely to shape what Codex is asked to emit. It never replaces, and is
never substituted for, canonical validation: `codex_provider.py` always
revalidates the returned JSON against the original, unmodified canonical
schema (`output_validation.validate_against_schema`) before accepting it.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping, NoReturn

from .errors import ProviderUnavailableError

CODE_TRANSPORT_SCHEMA_UNSUPPORTED = "CODEX_TRANSPORT_SCHEMA_UNSUPPORTED"

# Same bound-keyword set `anthropic_provider.py` strips for Claude's strict
# tool-use schema compiler — Codex's structured-output validator rejects the
# identical category of numeric/string/array bounds. Bounds remain fully
# enforced by canonical post-response validation; this only relaxes what the
# transport schema asks Codex's compiler to accept up front.
_UNSUPPORTED_BOUND_KEYWORDS = frozenset(
    {
        "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
        "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "pattern",
    }
)

# Pure metadata keywords: never affect validation outcome, safe to drop.
_METADATA_KEYWORDS_TO_DROP = frozenset({"$schema", "title", "description", "default", "examples"})

# Constructs this normalizer does not attempt to transform — none of the
# canonical role/decision schemas currently use any of these, so encountering
# one means either a future canonical-schema change this normalizer has not
# been taught, or an adversarial/unexpected input. Fail closed rather than
# guess at a transformation.
_UNSUPPORTED_CONSTRUCTS = frozenset(
    {
        "oneOf", "anyOf", "allOf", "not", "if", "then", "else",
        "patternProperties", "unevaluatedProperties", "dependentSchemas",
        "dependentRequired", "contains", "minContains", "maxContains",
        "propertyNames", "format",
    }
)

_MAX_DEPTH = 25


def _fail(message: str) -> NoReturn:
    raise ProviderUnavailableError(message, code=CODE_TRANSPORT_SCHEMA_UNSUPPORTED, retryable=False)


def _resolve_ref(ref: object, *, defs: Mapping[str, Any], ref_stack: tuple[str, ...]) -> tuple[Any, tuple[str, ...]]:
    if not isinstance(ref, str) or not (ref.startswith("#/$defs/") or ref.startswith("#/definitions/")):
        _fail("Codex transport schema does not support this $ref form")
    if ref in ref_stack:
        _fail("Codex transport schema contains a recursive $ref")
    name = ref.rsplit("/", 1)[-1]
    if name not in defs:
        _fail("Codex transport schema $ref target is missing")
    return defs[name], ref_stack + (ref,)


def _normalize_node(node: Any, *, defs: Mapping[str, Any], depth: int, ref_stack: tuple[str, ...]) -> Any:
    if depth > _MAX_DEPTH:
        _fail("Codex transport schema exceeds the maximum supported nesting depth")
    if isinstance(node, list):
        return [_normalize_node(item, defs=defs, depth=depth + 1, ref_stack=ref_stack) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        target, next_stack = _resolve_ref(node["$ref"], defs=defs, ref_stack=ref_stack)
        return _normalize_node(target, defs=defs, depth=depth + 1, ref_stack=next_stack)

    for construct in _UNSUPPORTED_CONSTRUCTS:
        if construct in node:
            _fail(f"Codex transport schema does not support the '{construct}' construct")
    if isinstance(node.get("additionalProperties"), dict):
        _fail("Codex transport schema does not support a schema-valued additionalProperties")
    enum_values = node.get("enum")
    if isinstance(enum_values, list) and any(not isinstance(v, (str, type(None))) for v in enum_values):
        _fail("Codex transport schema only supports string (or null) enum members")

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _METADATA_KEYWORDS_TO_DROP or key in _UNSUPPORTED_BOUND_KEYWORDS:
            continue
        result[key] = _normalize_node(value, defs=defs, depth=depth + 1, ref_stack=ref_stack)

    properties = result.get("properties")
    if isinstance(properties, dict) and properties:
        result["required"] = sorted(properties.keys())
        additional = result.get("additionalProperties", False)
        if additional is not False:
            _fail("Codex transport schema requires additionalProperties=false wherever properties are declared")
        result["additionalProperties"] = False

    return result


def build_codex_transport_schema(canonical_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Builds a deep-copied, deterministic Codex-compatible transport schema
    from `canonical_schema` without mutating the input. Fails closed
    (`ProviderUnavailableError`, code `CODEX_TRANSPORT_SCHEMA_UNSUPPORTED`)
    on any construct it cannot safely represent, including recursive or
    remote `$ref`s. Repeated calls on the same input are byte-identical once
    serialized with `sort_keys=True`."""
    if not isinstance(canonical_schema, Mapping):
        _fail("Codex transport schema input must be a JSON Schema object")
    root = copy.deepcopy(dict(canonical_schema))
    defs: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        value = root.get(key)
        if isinstance(value, dict):
            defs.update(value)
    root.pop("$defs", None)
    root.pop("definitions", None)
    normalized = _normalize_node(root, defs=defs, depth=0, ref_stack=())
    if not isinstance(normalized, dict):
        _fail("Codex transport schema root must be an object")
    return normalized


def transport_schema_byte_size(transport_schema: Mapping[str, Any]) -> int:
    text = json.dumps(transport_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


__all__ = [
    "CODE_TRANSPORT_SCHEMA_UNSUPPORTED",
    "build_codex_transport_schema",
    "transport_schema_byte_size",
]
