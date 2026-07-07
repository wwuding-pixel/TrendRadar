# coding=utf-8
"""Delivery audit helpers for notification and knowledge-base uploads."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


LATEST_REPORT = Path("output") / "notifications" / "latest" / "delivery-report.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _github_context() -> Dict[str, str]:
    keys = [
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_WORKFLOW",
        "GITHUB_JOB",
        "GITHUB_REF_NAME",
        "GITHUB_SHA",
        "GITHUB_ACTOR",
    ]
    return {key.lower(): os.getenv(key, "") for key in keys if os.getenv(key)}


def _load_report() -> Dict[str, Any]:
    if LATEST_REPORT.exists():
        try:
            return json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "schema_version": 1,
        "created_at": _now_iso(),
        "github": _github_context(),
        "notifications": {},
        "ima": {},
        "events": [],
    }


def _write_report(report: Dict[str, Any]) -> None:
    report["updated_at"] = _now_iso()
    report.setdefault("github", {}).update(_github_context())
    LATEST_REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp_report = LATEST_REPORT.with_suffix(".json.tmp")
    tmp_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_report.replace(LATEST_REPORT)


def _merge_dict(target: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def update_delivery_report(section: str, updates: Dict[str, Any]) -> None:
    report = _load_report()
    section_data = report.setdefault(section, {})
    _merge_dict(section_data, deepcopy(updates))
    _write_report(report)


def update_channel_report(channel: str, updates: Dict[str, Any]) -> None:
    report = _load_report()
    channels = report.setdefault("notifications", {})
    channel_data = channels.setdefault(channel, {})
    _merge_dict(channel_data, deepcopy(updates))
    _write_report(report)


def append_delivery_event(stage: str, status: str, **details: Any) -> None:
    report = _load_report()
    report.setdefault("events", []).append(
        {
            "time": _now_iso(),
            "stage": stage,
            "status": status,
            "details": details,
        }
    )
    _write_report(report)
