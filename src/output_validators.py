"""Validate agent outputs before they are saved or shown in the UI."""

from __future__ import annotations

import json
import re
from typing import Any

from .config_loader import load_config
from .content_schema import CONTENT_SCHEMA


def _output_is_blank(output: str | None) -> bool:
    return not output or not output.strip()


def _validate_json_against_schema(data: Any, schema: dict, path: str = "") -> tuple[bool, str]:
    """Validate a parsed JSON value against a subset of JSON Schema.

    Supports the constructs used by our agent schemas: object, array, string,
    number, boolean, required fields, additionalProperties, and nested items.
    """
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(data, dict):
            return False, f"{path or 'root'} ต้องเป้น object"
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                return False, f"{path} ขาดฟีลด์: {key}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    return False, f"{path} มีฟีลด์เกิน: {key}"
        for key, value in data.items():
            if key in properties:
                child_path = f"{path}.{key}" if path else key
                ok, err = _validate_json_against_schema(value, properties[key], child_path)
                if not ok:
                    return False, err
        return True, ""
    if stype == "array":
        if not isinstance(data, list):
            return False, f"{path} ต้องเป้น array"
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                ok, err = _validate_json_against_schema(item, items_schema, f"{path}[{i}]")
                if not ok:
                    return False, err
        return True, ""
    if stype == "string":
        if not isinstance(data, str):
            return False, f"{path} ต้องเป้น string"
        return True, ""
    if stype == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            return False, f"{path} ต้องเป้น number"
        return True, ""
    if stype == "boolean":
        if not isinstance(data, bool):
            return False, f"{path} ต้องเป้น boolean"
        return True, ""
    return True, ""


def _validate_json(output: str) -> tuple[bool, str]:
    if _output_is_blank(output):
        return False, "output ว่างเปล่า"
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return False, f"ไม่ใช่ JSON: {e}"
    return _validate_json_against_schema(data, CONTENT_SCHEMA["schema"])


def _validate_markdown(output: str, required_sections: list[str] | None = None) -> tuple[bool, str]:
    if _output_is_blank(output):
        return False, "output ว่างเปล่า"
    for s in required_sections or []:
        # ยอมรับหัวข้อแบบ: ชื่อสินค้า:, ## ชื่อสินค้า, **ชื่อสินค้า**, 1. **ชื่อสินค้า**,
        # 1. **ชื่อสินค้า (Product Name)** — ... (ชื่อ section ตามด้วยขอบเขตของคำ:
        # ช่องว่าง / ** / : / — / - / จบบรรทัด ถือว่าเป็นหัวข้อ section นั้น)
        pattern = re.compile(
            r"(?:^|\n)(?:#+\s*|\d+\.\s+\*\*|\*\*)?" + re.escape(s) + r"(?:\s|$|\*\*|[\*:—\-])",
            re.MULTILINE,
        )
        if not pattern.search(output):
            return False, f"ไม่พบ section: {s}"
    return True, ""


def validate_output(
    agent_name: str,
    output: str,
    required_sections: list[str] | None = None,
) -> tuple[bool, str]:
    """Return (ok, error_message) for an agent's raw text output."""
    cfg = load_config()
    agent_cfg = cfg.get("agents", {}).get(agent_name, {})
    required = required_sections if required_sections is not None else agent_cfg.get("required_output_sections")
    if required:
        return _validate_markdown(output, required)
    if agent_cfg.get("output_format") == "json":
        return _validate_json(output)
    return True, ""
