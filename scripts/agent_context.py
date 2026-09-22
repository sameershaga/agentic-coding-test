#!/usr/bin/env python3
"""Deterministic, token-budgeted context packets from validated memory."""

from __future__ import annotations

from agent_memory import MemoryStore, _clean_text


def estimate_tokens(text: str) -> int:
    """Approximate tokens as ceil(UTF-8 bytes / 4), without a tokenizer."""
    return (len(text.encode("utf-8")) + 3) // 4


def _memory_line(result: dict) -> str:
    record = result["record"]
    tags = ",".join(record["tags"]) or "none"
    provenance = record["provenance"]
    sources = ",".join(
        f"{key}={provenance[key]}"
        for key in ("source_run", "source_commit", "branch")
        if key in provenance
    ) or "none"
    return (
        f"- [{record['type']}] {record['summary']} "
        f"(id={record['id']}; scope={record['scope']}; tags={tags}; "
        f"confidence={record['confidence']:.2f}; provenance={sources})"
    )


def _with_footer(body: str, budget: int) -> str:
    tokens = 0
    while True:
        packet = f"{body}\nApproximate tokens: {tokens}\nBudget: {budget}"
        updated = estimate_tokens(packet)
        if updated == tokens:
            return packet
        tokens = updated


def build_context(store: MemoryStore, task: str, budget: int) -> dict:
    """Build a reproducible packet whose approximate size does not exceed budget."""
    task = _clean_text(task, "task")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("budget must be a positive integer")

    results, malformed = store.search(task)
    heading = f"PROJECT CONTEXT\nTask: {task}\nRelevant shared memory:"

    # A pathological task can consume the whole budget. Truncate it explicitly,
    # preserving the packet label and accounting footer.
    minimum = "PROJECT CONTEXT\nRelevant shared memory: none"
    packet = _with_footer(minimum, budget)
    if estimate_tokens(packet) > budget:
        packet = "PROJECT CONTEXT"
        while packet and estimate_tokens(packet) > budget:
            packet = packet[:-1]
        return {"packet": packet, "estimated_tokens": estimate_tokens(packet), "budget": budget,
                "included_ids": [], "malformed": malformed}

    body = heading
    included_ids: list[str] = []
    for result in results:
        candidate = body + "\n" + _memory_line(result)
        provisional = _with_footer(candidate, budget)
        if estimate_tokens(provisional) <= budget:
            body = candidate
            included_ids.append(result["record"]["id"])

    if not included_ids:
        body = heading + " none"

    packet = _with_footer(body, budget)

    # If the task itself overflowed the budget, fall back to the safe minimal packet.
    if estimate_tokens(packet) > budget:
        packet = _with_footer(minimum, budget)
        included_ids = []
    return {
        "packet": packet,
        "estimated_tokens": estimate_tokens(packet),
        "budget": budget,
        "included_ids": included_ids,
        "malformed": malformed,
    }
