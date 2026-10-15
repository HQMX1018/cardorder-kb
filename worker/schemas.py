"""Codex --output-schema 用的 JSON Schema（OpenAI 严格模式：所有字段 required、additionalProperties=false）。"""

ANSWER = {
    "type": "object", "additionalProperties": False,
    "required": ["plain", "detail", "evidence", "mismatches", "confidence", "followups"],
    "properties": {
        "plain": {"type": "string"},
        "detail": {"type": "string"},
        "evidence": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "line", "quote"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}}}},
        "mismatches": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "followups": {"type": "array", "items": {"type": "string"}},
    },
}

CLARIFY = {
    "type": "object", "additionalProperties": False,
    "required": ["hit_entry_id", "questions", "draft_card"],
    "properties": {
        "hit_entry_id": {"type": "string"},
        "questions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["q", "options", "allow_other"],
            "properties": {"q": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}},
                           "allow_other": {"type": "boolean"}}}},
        "draft_card": {"type": "object", "additionalProperties": False,
                       "required": ["page", "intent", "audience", "summary"],
                       "properties": {k: {"type": "string"} for k in ["page", "intent", "audience", "summary"]}},
    },
}
