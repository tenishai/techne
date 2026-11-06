"""Render the pattern relation graph as Mermaid or DOT (techne#305).

Pure functions over the dict from ``patterns.graph_data()``: nodes clustered by
``topic``, superseded nodes de-emphasized, edges labeled and styled by relation
kind (``supersedes`` dashed). No db access here — data in, string out, so it is
trivially testable and the CLI just picks a format.
"""

from __future__ import annotations


def _nid(pattern_id: str) -> str:
    """Node id safe for both Mermaid and DOT. Pattern ids are 16-hex and may start
    with a digit (illegal as a bare Mermaid id), so prefix with a letter."""
    return "n" + pattern_id


def _by_topic(nodes: list[dict]) -> dict[str, list[dict]]:
    """Group nodes by topic (NULL → '(untopiced)'), preserving encounter order."""
    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(n.get("topic") or "(untopiced)", []).append(n)
    return groups


def _mermaid_escape(s: str) -> str:
    # Inside a `["..."]` label only an unescaped quote breaks the parse.
    return (s or "").replace('"', "&quot;").replace("\n", " ")


def render_mermaid(data: dict) -> str:
    """Mermaid `flowchart` — renders inline on GitHub and most markdown viewers."""
    nodes, edges = data["nodes"], data["edges"]
    lines = ["flowchart LR"]
    superseded: list[str] = []
    for i, (topic, members) in enumerate(_by_topic(nodes).items()):
        lines.append(f'  subgraph t{i}["{_mermaid_escape(topic)}"]')
        for n in members:
            lines.append(f'    {_nid(n["id"])}["{_mermaid_escape(n["name"])}"]')
            if n.get("status") in ("superseded", "retired"):
                superseded.append(_nid(n["id"]))
        lines.append("  end")
    for e in edges:
        # supersedes reads as a dashed edge; everything else solid, all labeled.
        arrow = "-.->" if e["kind"] == "supersedes" else "-->"
        lines.append(f'  {_nid(e["src"])} {arrow}|{e["kind"]}| {_nid(e["dst"])}')
    if superseded:
        lines.append("  classDef superseded stroke-dasharray:4 3,opacity:0.55;")
        lines.append(f"  class {','.join(superseded)} superseded;")
    return "\n".join(lines)


def _dot_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# Per-kind edge styling for DOT (Graphviz). Unlisted kinds (e.g. 'related') use
# the default solid black edge.
_DOT_EDGE_STYLE = {
    "supersedes": 'style="dashed", color="gray50"',
    "composes-with": 'color="steelblue"',
    "variant-of": 'color="seagreen"',
}


def render_dot(data: dict) -> str:
    """Graphviz DOT — for `dot -Tsvg` / `dot -Tpng` rendering."""
    nodes, edges = data["nodes"], data["edges"]
    lines = ["digraph patterns {", "  rankdir=LR;", "  node [shape=box];"]
    for i, (topic, members) in enumerate(_by_topic(nodes).items()):
        lines.append(f"  subgraph cluster_{i} {{")
        lines.append(f'    label="{_dot_escape(topic)}";')
        for n in members:
            attrs = f'label="{_dot_escape(n["name"])}"'
            if n.get("status") in ("superseded", "retired"):
                attrs += ', style="dashed", color="gray60", fontcolor="gray40"'
            lines.append(f'    "{_nid(n["id"])}" [{attrs}];')
        lines.append("  }")
    for e in edges:
        style = _DOT_EDGE_STYLE.get(e["kind"], "")
        attrs = f'label="{e["kind"]}"' + (f", {style}" if style else "")
        lines.append(f'  "{_nid(e["src"])}" -> "{_nid(e["dst"])}" [{attrs}];')
    lines.append("}")
    return "\n".join(lines)
