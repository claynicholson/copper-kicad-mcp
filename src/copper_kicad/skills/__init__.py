"""Copper's agent skills (``<name>/SKILL.md``, plain markdown), shipped three ways: as
plugin skills for the clients that support them (copied into ``plugin/skills`` at build
time), as MCP prompts, and readable here for any other consumer.

    list_skills() -> [{"name", "description", "path", "body"}]
    get(name) -> dict | None
"""
from __future__ import annotations

import re
from pathlib import Path

DIR = Path(__file__).resolve().parent


def _parse(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            meta[k.strip()] = v.strip().strip('"')
    return meta, m.group(2)


def list_skills() -> list[dict]:
    out = []
    for p in sorted(DIR.glob("*/SKILL.md")):
        meta, body = _parse(p.read_text(encoding="utf-8"))
        out.append({"name": meta.get("name", p.parent.name), "description": meta.get("description", ""),
                    "path": str(p), "body": body})
    return out


def get(name: str) -> dict | None:
    return next((s for s in list_skills() if s["name"] == name), None)
