#!/usr/bin/env python3
"""Analyze local Codex JSONL token ledgers without modifying them.

The parser treats total_token_usage records as cumulative counters. It groups
records by logical session id, removes exact duplicate snapshots, and sums
counter deltas. If a counter decreases, the new value starts a fresh segment.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
PRIMARY_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
UUID_AT_END = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$", re.I)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def timestamp_text(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    return 0


def normalize_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    usage = {field: integer(raw.get(field, 0)) for field in TOKEN_FIELDS}
    if usage["total_tokens"] == 0 and (usage["input_tokens"] or usage["output_tokens"]):
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def blank_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def add_usage(target: dict[str, int], source: dict[str, int]) -> None:
    for field in TOKEN_FIELDS:
        target[field] += source[field]


def subtract_usage(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    return {field: current[field] - previous[field] for field in TOKEN_FIELDS}


def usage_tuple(usage: dict[str, int]) -> tuple[int, ...]:
    return tuple(usage[field] for field in TOKEN_FIELDS)


def source_label(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        if "subagent" in source:
            value = source["subagent"]
            if isinstance(value, dict):
                spawn = value.get("thread_spawn")
                if isinstance(spawn, dict):
                    detail = spawn.get("agent_nickname") or spawn.get("agent_path") or "thread_spawn"
                else:
                    detail = next((str(v) for v in value.values() if v), "unspecified")
            else:
                detail = str(value)
            return f"subagent:{detail}"
        return json.dumps(source, sort_keys=True, separators=(",", ":"))
    return "unknown"


@dataclass
class Event:
    session_id: str
    timestamp: datetime
    cumulative: dict[str, int]
    last_usage: dict[str, int] | None
    model: str | None
    path: str
    order: int
    inherited_baseline: bool = False


@dataclass
class Session:
    session_id: str
    paths: set[str] = field(default_factory=set)
    parent_ids: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    cli_versions: set[str] = field(default_factory=set)
    known_models: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)
    aggregate: dict[str, int] = field(default_factory=blank_usage)
    start: datetime | None = None
    end: datetime | None = None
    resets: int = 0
    duplicate_snapshots: int = 0


def relevant_record(line: str) -> bool:
    return (
        '"type":"session_meta"' in line
        or '"type": "session_meta"' in line
        or '"type":"turn_context"' in line
        or '"type": "turn_context"' in line
        or '"type":"token_count"' in line
        or '"type": "token_count"' in line
    )


def jsonl_files(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    seen_paths: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            resolved = str(path.resolve()).casefold()
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                found.append(path)
    return sorted(found, key=lambda p: str(p).casefold())


def parse_logs(paths: list[Path]) -> tuple[dict[str, Session], dict[str, Any]]:
    sessions: dict[str, Session] = {}
    diagnostics: dict[str, Any] = {
        "files_scanned": len(paths),
        "bytes_scanned": sum(path.stat().st_size for path in paths),
        "lines_scanned": 0,
        "relevant_records": 0,
        "malformed_relevant_records": 0,
        "files_without_token_records": 0,
        "embedded_session_meta_records": 0,
        "inherited_history_token_snapshots": 0,
        "token_info_shapes": Counter(),
        "usage_shapes": Counter(),
        "session_meta_shapes": Counter(),
        "cli_versions": Counter(),
    }
    order = 0

    for file_number, path in enumerate(paths, 1):
        match = UUID_AT_END.search(path.name)
        fallback_id = match.group(1) if match else f"file:{path.name}"
        current_id = fallback_id
        current_model: str | None = None
        first_meta_seen = False
        file_start: datetime | None = None
        file_has_parent = False
        file_token_records = 0

        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                diagnostics["lines_scanned"] += 1
                if not relevant_record(line):
                    continue
                diagnostics["relevant_records"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics["malformed_relevant_records"] += 1
                    continue

                record_type = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record_type == "session_meta":
                    diagnostics["session_meta_shapes"][tuple(sorted(payload))] += 1
                    if not first_meta_seen:
                        # A spawned child log can embed the parent's historical
                        # session_meta records. The first record (and filename)
                        # identify this file's independent ledger; later meta
                        # records are replayed context and must not switch ids.
                        candidate = payload.get("id") or current_id
                        current_id = str(candidate)
                        first_meta_seen = True
                        file_start = parse_timestamp(record.get("timestamp"))
                        session = sessions.setdefault(current_id, Session(current_id))
                        session.paths.add(str(path))
                        parent_id = payload.get("parent_thread_id")
                        if parent_id:
                            file_has_parent = True
                            session.parent_ids.add(str(parent_id))
                        session.sources.add(source_label(payload.get("source")))
                        version = payload.get("cli_version")
                        if version:
                            session.cli_versions.add(str(version))
                            diagnostics["cli_versions"][str(version)] += 1
                    else:
                        diagnostics["embedded_session_meta_records"] += 1
                    continue

                session = sessions.setdefault(current_id, Session(current_id))
                session.paths.add(str(path))

                if record_type == "turn_context":
                    model = payload.get("model")
                    if model:
                        current_model = str(model)
                        session.known_models.add(current_model)
                    continue

                if record_type != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                diagnostics["token_info_shapes"][tuple(sorted(info))] += 1
                cumulative_raw = info.get("total_token_usage")
                cumulative = normalize_usage(cumulative_raw)
                if cumulative is None:
                    continue
                last_usage = normalize_usage(info.get("last_token_usage"))
                diagnostics["usage_shapes"][tuple(sorted(cumulative_raw))] += 1
                timestamp = parse_timestamp(record.get("timestamp"))
                if timestamp is None:
                    continue
                order += 1
                # Replayed history is re-stamped in a millisecond burst just
                # after the child session_meta record. Genuine first model
                # completions in sampled files arrive seconds later.
                inherited_baseline = bool(
                    file_has_parent
                    and file_start
                    and file_start <= timestamp <= file_start + timedelta(seconds=1)
                )
                if inherited_baseline:
                    diagnostics["inherited_history_token_snapshots"] += 1
                session.events.append(
                    Event(
                        current_id,
                        timestamp,
                        cumulative,
                        last_usage,
                        current_model,
                        str(path),
                        order,
                        inherited_baseline,
                    )
                )
                session.start = timestamp if session.start is None else min(session.start, timestamp)
                session.end = timestamp if session.end is None else max(session.end, timestamp)
                file_token_records += 1

        if file_token_records == 0:
            diagnostics["files_without_token_records"] += 1
        if file_number % 25 == 0 or file_number == len(paths):
            print(f"Parsed {file_number}/{len(paths)} files", file=sys.stderr, flush=True)

    for name in ("token_info_shapes", "usage_shapes", "session_meta_shapes", "cli_versions"):
        counter = diagnostics[name]
        if name == "cli_versions":
            diagnostics[name] = dict(sorted(counter.items()))
        else:
            diagnostics[name] = [
                {"fields": list(fields), "records": count}
                for fields, count in counter.most_common()
            ]
    return sessions, diagnostics


def process_sessions(sessions: dict[str, Session], diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    delta_records: list[dict[str, Any]] = []
    total_resets = 0
    total_duplicates = 0
    total_inconsistencies = 0
    validated_delta_records = 0
    last_usage_mismatches = 0
    validated_delta_total = 0
    validated_last_usage_total = 0
    repeated_snapshot_last_usage_records = 0
    repeated_snapshot_last_usage_tokens = 0
    nonzero_delta_last_usage_mismatches = 0

    for session in sessions.values():
        session.events.sort(key=lambda event: (event.timestamp, event.order))
        seen: set[tuple[str, tuple[int, ...]]] = set()
        previous: dict[str, int] | None = None
        unique_model = next(iter(session.known_models)) if len(session.known_models) == 1 else None

        for event in session.events:
            signature = (timestamp_text(event.timestamp) or "", usage_tuple(event.cumulative))
            if signature in seen:
                session.duplicate_snapshots += 1
                continue
            seen.add(signature)

            reset = previous is None or any(event.cumulative[field] < previous[field] for field in PRIMARY_FIELDS)
            if reset:
                delta = dict(event.cumulative)
                if previous is not None:
                    session.resets += 1
            else:
                delta = subtract_usage(event.cumulative, previous)
            previous = event.cumulative

            # Spawned ledgers can replay the complete parent history at the
            # child's creation timestamp. Those snapshots establish the
            # inherited cumulative baseline but are not new child usage.
            if event.inherited_baseline:
                continue

            if delta["total_tokens"] != delta["input_tokens"] + delta["output_tokens"]:
                total_inconsistencies += 1
            if event.last_usage is not None:
                validated_delta_records += 1
                validated_delta_total += delta["total_tokens"]
                validated_last_usage_total += event.last_usage["total_tokens"]
                if usage_tuple(delta) != usage_tuple(event.last_usage):
                    last_usage_mismatches += 1
                    if not any(delta.values()) and event.last_usage["total_tokens"]:
                        repeated_snapshot_last_usage_records += 1
                        repeated_snapshot_last_usage_tokens += event.last_usage["total_tokens"]
                    elif any(delta.values()):
                        nonzero_delta_last_usage_mismatches += 1
            if not any(delta.values()):
                continue

            model = event.model or unique_model or "unknown"
            add_usage(session.aggregate, delta)
            delta_records.append(
                {
                    "session_id": session.session_id,
                    "timestamp": event.timestamp,
                    "model": model,
                    "usage": delta,
                }
            )

        total_resets += session.resets
        total_duplicates += session.duplicate_snapshots

    diagnostics["logical_sessions"] = len(sessions)
    diagnostics["sessions_with_token_records"] = sum(bool(session.events) for session in sessions.values())
    diagnostics["cumulative_counter_resets"] = total_resets
    diagnostics["exact_duplicate_snapshots_removed"] = total_duplicates
    diagnostics["delta_total_identity_mismatches"] = total_inconsistencies
    diagnostics["delta_records_validated_against_last_usage"] = validated_delta_records
    diagnostics["delta_vs_last_usage_mismatches"] = last_usage_mismatches
    diagnostics["validated_delta_total_tokens"] = validated_delta_total
    diagnostics["validated_last_usage_total_tokens"] = validated_last_usage_total
    diagnostics["repeated_snapshot_last_usage_records"] = repeated_snapshot_last_usage_records
    diagnostics["repeated_snapshot_last_usage_tokens_not_counted"] = repeated_snapshot_last_usage_tokens
    diagnostics["nonzero_delta_last_usage_mismatches"] = nonzero_delta_last_usage_mismatches
    return delta_records


def group_records(delta_records: list[dict[str, Any]], key_function) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = defaultdict(blank_usage)
    for record in delta_records:
        add_usage(grouped[key_function(record)], record["usage"])
    return dict(sorted(grouped.items()))


def public_usage(usage: dict[str, int]) -> dict[str, int]:
    result = dict(usage)
    result["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    return result


def child_independence(sessions: dict[str, Session], delta_records: list[dict[str, Any]]) -> dict[str, Any]:
    totals_by_session = {sid: session.aggregate["total_tokens"] for sid, session in sessions.items()}
    parent_children: dict[str, list[str]] = defaultdict(list)
    for sid, session in sessions.items():
        for parent_id in session.parent_ids:
            if parent_id in sessions:
                parent_children[parent_id].append(sid)

    conclusive: list[dict[str, Any]] = []
    for parent_id, child_ids in parent_children.items():
        parent_total = totals_by_session.get(parent_id, 0)
        children_total = sum(totals_by_session.get(child_id, 0) for child_id in child_ids)
        if children_total > parent_total:
            conclusive.append(
                {
                    "parent_session_id": parent_id,
                    "parent_total_tokens": parent_total,
                    "logged_children": len(child_ids),
                    "children_total_tokens": children_total,
                    "children_minus_parent": children_total - parent_total,
                }
            )

    child_sessions = [session for session in sessions.values() if session.parent_ids and session.events]
    child_total = sum(session.aggregate["total_tokens"] for session in child_sessions)
    return {
        "sessions_with_logged_parent_id": len(child_sessions),
        "child_session_total_tokens": child_total,
        "parents_present_locally": len(parent_children),
        "parents_where_logged_children_exceed_parent_total": len(conclusive),
        "examples": sorted(conclusive, key=lambda row: row["children_minus_parent"], reverse=True)[:10],
        "interpretation": (
            "Child files identify their own ledger with the first session_meta.id and may replay a parent's historical "
            "counter snapshots in a millisecond burst at spawn. The replay is used only as a baseline; only later child "
            "counter deltas are counted. Where logged child deltas exceed the parent's entire ledger, the parent cannot "
            "already contain those child deltas."
        ),
    }


def percentage(part: int, whole: int) -> float | None:
    return round(part * 100.0 / whole, 6) if whole else None


def compact_usage_row(key: str, usage: dict[str, int]) -> dict[str, Any]:
    result: dict[str, Any] = {"key": key}
    result.update(public_usage(usage))
    return result


def build_report(
    roots: list[Path],
    sessions: dict[str, Session],
    diagnostics: dict[str, Any],
    delta_records: list[dict[str, Any]],
    official_total: int,
) -> dict[str, Any]:
    aggregate = blank_usage()
    for record in delta_records:
        add_usage(aggregate, record["usage"])
    public_aggregate = public_usage(aggregate)

    by_day = group_records(delta_records, lambda row: row["timestamp"].astimezone(timezone.utc).date().isoformat())
    by_month = group_records(delta_records, lambda row: row["timestamp"].astimezone(timezone.utc).strftime("%Y-%m"))
    by_model = group_records(delta_records, lambda row: row["model"])

    token_times = [record["timestamp"] for record in delta_records]
    top_sessions: list[dict[str, Any]] = []
    for session in sorted(sessions.values(), key=lambda item: item.aggregate["total_tokens"], reverse=True):
        if not session.events:
            continue
        row: dict[str, Any] = {
            "session_id": session.session_id,
            "start_utc": timestamp_text(session.start),
            "end_utc": timestamp_text(session.end),
            "source": sorted(session.sources),
            "parent_session_ids": sorted(session.parent_ids),
            "models": sorted(session.known_models) or ["unknown"],
            "files": [Path(path).name for path in sorted(session.paths)],
            "counter_resets": session.resets,
            "duplicate_snapshots_removed": session.duplicate_snapshots,
        }
        row.update(public_usage(session.aggregate))
        top_sessions.append(row)

    total = aggregate["total_tokens"]
    input_total = aggregate["input_tokens"]
    cached = aggregate["cached_input_tokens"]
    output = aggregate["output_tokens"]
    total_only_legacy = total - input_total - output
    categories = {
        field: {
            "tokens": public_aggregate[field],
            "percent_of_total_tokens": percentage(public_aggregate[field], total),
        }
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
    }

    unknown_model_tokens = by_model.get("unknown", blank_usage())["total_tokens"]
    report = {
        "generated_at_utc": timestamp_text(datetime.now(timezone.utc)),
        "source_roots": [str(root) for root in roots],
        "methodology": {
            "counter_handling": (
                "Identify each independent ledger by the first session_meta.id in its file; later session_meta records "
                "can be replayed parent context and do not change ledger identity. Group total_token_usage snapshots by "
                "ledger id; sort by timestamp; remove exact duplicate snapshots; sum cumulative deltas; when a primary "
                "cumulative counter decreases, begin a new counter segment and add the new snapshot as that segment's value."
            ),
            "child_inherited_history": (
                "For a ledger with parent_thread_id, token snapshots re-stamped within one second of child creation are "
                "inherited parent-history replay. They establish the child's starting cumulative baseline but add zero "
                "new usage. Only subsequent child deltas are counted."
            ),
            "daily_monthly_attribution": "Attribute each cumulative delta to the UTC date of its token_count event.",
            "model_attribution": (
                "Use the most recent preceding turn_context.model in the same ledger. If a session has exactly one "
                "known model, backfill otherwise-unattributed deltas with that model; label the remainder unknown."
            ),
            "total_definition": (
                "Use the logged total_tokens field. In normal records it equals input_tokens plus output_tokens; a small "
                "set of legacy records reports only total_tokens, so their input/output split is unavailable. "
                "reasoning_output_tokens is a subset of output_tokens."
            ),
            "uncached_definition": "input_tokens minus cached_input_tokens.",
            "word_equivalent": "0.75 English words per token; approximation only.",
        },
        "date_range": {
            "earliest_token_event_utc": timestamp_text(min(token_times) if token_times else None),
            "latest_token_event_utc": timestamp_text(max(token_times) if token_times else None),
        },
        "aggregate": public_aggregate,
        "legacy_total_only_tokens_without_category_split": total_only_legacy,
        "category_percentages": categories,
        "cached_input_percentage": percentage(cached, input_total),
        "output_vs_cached_context": {
            "output_tokens": output,
            "cached_input_tokens": cached,
            "cached_minus_output_tokens": cached - output,
            "cached_tokens_per_output_token": round(cached / output, 6) if output else None,
            "output_as_percent_of_cached": percentage(output, cached),
        },
        "approximate_english_words": {
            "total_tokens": round(total * 0.75),
            "input_tokens": round(input_total * 0.75),
            "cached_input_tokens": round(cached * 0.75),
            "uncached_input_tokens": round(public_aggregate["uncached_input_tokens"] * 0.75),
            "output_tokens": round(output * 0.75),
            "reasoning_output_tokens": round(aggregate["reasoning_output_tokens"] * 0.75),
        },
        "official_usage_comparison": {
            "official_lifetime_total_tokens": official_total,
            "local_total_tokens": total,
            "difference_official_minus_local": official_total - total,
            "local_coverage_percent": percentage(total, official_total),
        },
        "by_day": [compact_usage_row(key, usage) for key, usage in by_day.items()],
        "by_month": [compact_usage_row(key, usage) for key, usage in by_month.items()],
        "by_model": [
            compact_usage_row(key, usage)
            | {"percent_of_local_total": percentage(usage["total_tokens"], total)}
            for key, usage in sorted(by_model.items(), key=lambda item: item[1]["total_tokens"], reverse=True)
        ],
        "model_attribution_quality": {
            "unknown_model_tokens": unknown_model_tokens,
            "unknown_model_percent": percentage(unknown_model_tokens, total),
        },
        "top_20_sessions": top_sessions[:20],
        "all_sessions": top_sessions,
        "child_session_analysis": child_independence(sessions, delta_records),
        "diagnostics": diagnostics,
    }
    return report


def n(value: int) -> str:
    return f"{value:,}"


def p(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def markdown_report(report: dict[str, Any], json_name: str) -> str:
    aggregate = report["aggregate"]
    categories = report["category_percentages"]
    comparison = report["official_usage_comparison"]
    child = report["child_session_analysis"]
    diagnostics = report["diagnostics"]
    output_cached = report["output_vs_cached_context"]
    words = report["approximate_english_words"]

    lines = [
        "---",
        "title: Codex Token Usage Analysis",
        "date: 2026-08-30",
        "tags:",
        "  - codex",
        "  - usage",
        "  - analysis",
        "---",
        "",
        "# Codex Token Usage Analysis",
        "",
        f"Local JSONL token ledgers cover **{report['date_range']['earliest_token_event_utc']}** through **{report['date_range']['latest_token_event_utc']}** (UTC). Raw machine-readable results: [{json_name}](<{json_name}>).",
        "",
        "## All-time local aggregate",
        "",
        "| Category | Tokens | % of total |",
        "|---|---:|---:|",
    ]
    labels = {
        "input_tokens": "Input",
        "cached_input_tokens": "Cached input",
        "uncached_input_tokens": "Uncached input",
        "output_tokens": "Output",
        "reasoning_output_tokens": "Reasoning output (subset of output)",
        "total_tokens": "Logged total",
    }
    for field, label in labels.items():
        lines.append(f"| {label} | {n(categories[field]['tokens'])} | {p(categories[field]['percent_of_total_tokens'])} |")

    lines.extend(
        [
            "",
            f"**Cached share of all input:** {p(report['cached_input_percentage'])}.",
            "",
            "The category percentages overlap: cached and uncached partition input, while reasoning output is already included in output. They should not be added together.",
            "",
            f"A legacy schema contributes **{n(report['legacy_total_only_tokens_without_category_split'])} total-only tokens** with no input/output category split. This is why the category rows do not sum to the reported total by that small amount.",
            "",
            "## Output versus cached-context rereading",
            "",
            f"Codex produced **{n(output_cached['output_tokens'])} output tokens** while rereading **{n(output_cached['cached_input_tokens'])} cached input tokens**. That is **{output_cached['cached_tokens_per_output_token']:.2f} cached tokens per output token**, or {n(output_cached['cached_minus_output_tokens'])} more cached-context tokens than output tokens.",
            "",
            "## Approximate English-word equivalent",
            "",
            f"At 0.75 words/token, the local total is approximately **{n(words['total_tokens'])} English words**. This is only a rough conversion; tokens and natural-language words are not interchangeable, and cached rereads are computational workload rather than newly written prose.",
            "",
            "## Official `/usage` comparison",
            "",
            f"The local ledgers total **{n(comparison['local_total_tokens'])} tokens**, compared with **{n(comparison['official_lifetime_total_tokens'])}** on the account-side `/usage` screen. Local logs cover **{p(comparison['local_coverage_percent'])}** of that figure, a difference of **{n(comparison['difference_official_minus_local'])} tokens**.",
            "",
            "Likely reasons for a shortfall include usage before the earliest retained local log, deleted or missing logs, sessions from another computer, cloud Codex work, and differences between local event logging and account-side metering. The local result was not adjusted to match the official figure.",
            "",
            "## Monthly aggregates",
            "",
            "| Month (UTC) | Input | Cached input | Uncached input | Output | Reasoning output | Total |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["by_month"]:
        lines.append(
            f"| {row['key']} | {n(row['input_tokens'])} | {n(row['cached_input_tokens'])} | "
            f"{n(row['uncached_input_tokens'])} | {n(row['output_tokens'])} | "
            f"{n(row['reasoning_output_tokens'])} | {n(row['total_tokens'])} |"
        )

    lines.extend(
        [
            "",
            "## Daily aggregates",
            "",
            "| Date (UTC) | Input | Cached input | Uncached input | Output | Reasoning output | Total |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["by_day"]:
        lines.append(
            f"| {row['key']} | {n(row['input_tokens'])} | {n(row['cached_input_tokens'])} | "
            f"{n(row['uncached_input_tokens'])} | {n(row['output_tokens'])} | "
            f"{n(row['reasoning_output_tokens'])} | {n(row['total_tokens'])} |"
        )

    lines.extend(
        [
            "",
            "## Usage by model",
            "",
            "| Model | Total tokens | % of local total | Input | Cached input | Output | Reasoning output |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["by_model"]:
        lines.append(
            f"| {row['key']} | {n(row['total_tokens'])} | {p(row['percent_of_local_total'])} | "
            f"{n(row['input_tokens'])} | {n(row['cached_input_tokens'])} | {n(row['output_tokens'])} | "
            f"{n(row['reasoning_output_tokens'])} |"
        )
    lines.append("")
    lines.append(
        f"Model association is direct or single-model backfilled for all but **{p(report['model_attribution_quality']['unknown_model_percent'])}** of local tokens."
    )

    lines.extend(
        [
            "",
            "## Top 20 sessions",
            "",
            "| Rank | Session | Start (UTC) | Model(s) | Source | Total tokens |",
            "|---:|---|---|---|---|---:|",
        ]
    )
    for rank, row in enumerate(report["top_20_sessions"], 1):
        lines.append(
            f"| {rank} | `{row['session_id']}` | {row['start_utc']} | {', '.join(row['models'])} | "
            f"{', '.join(row['source']) or 'unknown'} | {n(row['total_tokens'])} |"
        )

    lines.extend(
        [
            "",
            "## Method and double-counting checks",
            "",
            "Each `token_count.info.total_token_usage` record was treated as a cumulative snapshot, not an independent charge. The first `session_meta.id` in each file identifies that file's independent ledger; later metadata can be replayed parent context. Records were grouped by ledger ID and ordered by timestamp. Exact duplicate snapshots were removed. Within a monotonic counter segment, only the difference from the previous snapshot was added. When a primary cumulative category decreased, the new snapshot began a fresh segment, preserving legitimate usage across counter resets.",
            "",
            "Daily and monthly totals use the timestamp of each calculated delta, so long-running sessions crossing date boundaries are not assigned wholly to their start date. Models use the most recent preceding `turn_context.model`; a session-wide single-model value fills earlier gaps. Remaining ambiguity is labeled `unknown`.",
            "",
            f"There are **{child['sessions_with_logged_parent_id']} token-bearing child sessions** totaling **{n(child['child_session_total_tokens'])} new tokens after inherited baselines**. The parser excluded **{n(diagnostics['inherited_history_token_snapshots'])} replayed parent-history snapshots** emitted in the one-second spawn burst. In **{child['parents_where_logged_children_exceed_parent_total']} parent ledgers**, logged child deltas alone exceed the parent's entire cumulative total, which would be impossible if those child deltas were already rolled into the parent. Each branch's new usage was therefore counted once.",
            "",
            f"Diagnostics: {diagnostics['files_scanned']} files, {n(diagnostics['bytes_scanned'])} bytes, {diagnostics['sessions_with_token_records']} token-bearing independent ledgers, {n(diagnostics['exact_duplicate_snapshots_removed'])} exact duplicate snapshots removed, {n(diagnostics['inherited_history_token_snapshots'])} inherited snapshots excluded, {diagnostics['cumulative_counter_resets']} counter resets, and {diagnostics['malformed_relevant_records']} malformed relevant records. The computed cumulative delta was also compared with `last_token_usage` for {n(diagnostics['delta_records_validated_against_last_usage'])} records. Of {n(diagnostics['delta_vs_last_usage_mismatches'])} differences, {n(diagnostics['repeated_snapshot_last_usage_records'])} were unchanged cumulative snapshots that repeated a prior nonzero `last_token_usage`; summing that field would have overcounted {n(diagnostics['repeated_snapshot_last_usage_tokens_not_counted'])} tokens. Only {n(diagnostics['nonzero_delta_last_usage_mismatches'])} nonzero deltas differed from `last_token_usage`.",
            "",
            "## Schema observations",
            "",
            "The inspected installations consistently store token counts as `event_msg` records with `payload.type = token_count`. Their `info` object contains `total_token_usage` and `last_token_usage`; the former is cumulative and the latter is per response. Token categories observed include input, cached input, cache-write input, output, reasoning output, and total. Session metadata and turn-context fields vary by Codex version, so the parser keys only off the fields required for this calculation.",
            "",
            "The category meanings align with the [official OpenAI Responses usage schema](https://developers.openai.com/api/reference/cli/resources/responses/methods/create); cumulative local-ledger behavior and spawned-history replay were derived empirically from these JSONL files because they are not specified by that API reference.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path, help="Session-log roots to scan recursively")
    parser.add_argument("--official-total", type=int, default=5_350_000_000)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    paths = jsonl_files(args.roots)
    if not paths:
        parser.error("No JSONL files found under the supplied roots")

    sessions, diagnostics = parse_logs(paths)
    delta_records = process_sessions(sessions, diagnostics)
    report = build_report(args.roots, sessions, diagnostics, delta_records, args.official_total)

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.markdown_output.write_text(markdown_report(report, args.json_output.name), encoding="utf-8")
    print(json.dumps(report["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
