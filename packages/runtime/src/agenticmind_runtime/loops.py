"""Loop regions.

A loop is a pair of nodes: a *loop start* (``runtime_role="loop_start"``) and a *loop end*
(``runtime_role="loop_end"``) connected by a ``scope`` data edge. The **body** is every node that both
depends on the start's body outputs (anything but ``scope``) and feeds the end's inputs. The runtime
executes the body once per iteration in its own scope.

Rules (validation):
* every loop start has exactly one loop end, and vice versa (``LOOP_UNPAIRED``, ``LOOP_SCOPE_SHARED``);
* values produced in a body may only leave it through the loop end (``LOOP_REGION_LEAK``);
* two loops are either disjoint or properly nested (``LOOP_OVERLAP``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from agenticmind_ir import Edge, EdgeKind

SCOPE_PORT = "scope"


@dataclass(frozen=True)
class LoopPair:
    start: str
    end: str
    body: frozenset[str]  # all body nodes, including nested loops' nodes (excludes start/end)


@dataclass
class LoopAnalysis:
    pairs: list[LoopPair]
    unpaired_starts: list[str]
    shared_scopes: list[str]
    leaks: list[tuple[str, Edge]]  # (loop start, offending edge)
    overlaps: list[tuple[str, str]]


def _bfs(seeds: Iterable[str], adjacency: dict[str, set[str]], stop: str) -> set[str]:
    seen: set[str] = set()
    stack = [s for s in seeds if s != stop]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for m in adjacency.get(n, ()):
            if m != stop and m not in seen:
                stack.append(m)
    return seen


def analyze_loops(roles: dict[str, str | None], edges: list[Edge]) -> LoopAnalysis:
    """``roles`` maps node id → runtime role (``loop_start``/``loop_end``/None)."""
    starts = [n for n, r in roles.items() if r == "loop_start"]
    ends = {n for n, r in roles.items() if r == "loop_end"}
    scope_edges = [
        e for e in edges if e.kind == EdgeKind.DATA and e.source.port == SCOPE_PORT and e.target.port == SCOPE_PORT
    ]
    by_start: dict[str, list[str]] = defaultdict(list)
    for e in scope_edges:
        if e.source.node in roles and roles[e.source.node] == "loop_start" and e.target.node in ends:
            by_start[e.source.node].append(e.target.node)

    forward: dict[str, set[str]] = defaultdict(set)
    backward: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        forward[e.source.node].add(e.target.node)
        backward[e.target.node].add(e.source.node)

    pairs: list[LoopPair] = []
    unpaired: list[str] = []
    shared: list[str] = []
    leaks: list[tuple[str, Edge]] = []
    for start in sorted(starts):
        targets = by_start.get(start, [])
        if not targets:
            unpaired.append(start)
            continue
        if len(targets) > 1:
            shared.append(start)
            continue
        end = targets[0]
        body_seeds = [
            e.target.node
            for e in edges
            if e.source.node == start and not (e.kind == EdgeKind.DATA and e.source.port == SCOPE_PORT)
        ]
        end_seeds = [
            e.source.node
            for e in edges
            if e.target.node == end and not (e.kind == EdgeKind.DATA and e.target.port == SCOPE_PORT)
        ]
        downstream = _bfs(body_seeds, forward, stop=end)
        upstream = _bfs(end_seeds, backward, stop=start)
        body = frozenset((downstream & upstream) - {start, end})
        for e in edges:
            src_in_body = e.source.node in body or (
                e.source.node == start and not (e.kind == EdgeKind.DATA and e.source.port == SCOPE_PORT)
            )
            if src_in_body and e.target.node not in body and e.target.node != end:
                leaks.append((start, e))
        pairs.append(LoopPair(start=start, end=end, body=body))

    paired_ends = {p.end for p in pairs}
    unpaired.extend(sorted(e for e in ends if e not in paired_ends and not any(e in by_start[s] for s in shared)))

    overlaps: list[tuple[str, str]] = []
    for i, a in enumerate(pairs):
        for b in pairs[i + 1 :]:
            a_all = a.body | {a.start, a.end}
            b_all = b.body | {b.start, b.end}
            if not (a_all & b_all):
                continue
            a_in_b = a.start in b.body and a.end in b.body and a.body <= b.body
            b_in_a = b.start in a.body and b.end in a.body and b.body <= a.body
            if not (a_in_b or b_in_a):
                overlaps.append((a.start, b.start))
    return LoopAnalysis(pairs=pairs, unpaired_starts=unpaired, shared_scopes=shared, leaks=leaks, overlaps=overlaps)


def direct_members(container: frozenset[str], pairs: list[LoopPair]) -> set[str]:
    """Nodes of ``container`` not hidden inside a loop nested directly or deeper within it.

    Nested loops are collapsed: their start and end stay (the end executes the whole inner loop), their
    body nodes are removed.
    """
    hidden: set[str] = set()
    for p in pairs:
        if p.start in container and p.end in container:
            hidden |= p.body
    return set(container) - hidden
