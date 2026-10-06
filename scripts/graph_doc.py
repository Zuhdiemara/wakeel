"""Writes docs/graph.md from the compiled LangGraph, so the diagram in the
docs is the real graph. CI fails if the file is out of date.

    python scripts/graph_doc.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wakeel.graph import Deps, make_graph
from wakeel.ledger import demo_ledger
from wakeel.rag import HashEmbedder, Index, load_corpus

ROOT = Path(__file__).resolve().parent.parent
g = make_graph(Deps(None, Index(load_corpus(ROOT / "corpus"), HashEmbedder()), demo_ledger()))
mermaid = g.get_graph().draw_mermaid()
(ROOT / "docs" / "graph.md").write_text(
    "# The agent graph\n\nGenerated from the compiled LangGraph by `scripts/graph_doc.py`; do not edit by hand.\n"
    "Solid edges are fixed; dotted edges are the supervisor's routing, a plain function of the state.\n\n```mermaid\n" + mermaid + "```\n")
print("wrote docs/graph.md")
