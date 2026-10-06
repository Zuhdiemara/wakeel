"""Wakeel's tools over the Model Context Protocol, for any MCP client.

    WAKEEL_CUSTOMER=sara python -m wakeel.mcp_server        # stdio

The same guarantees as inside the graph: the customer is fixed by the
server's configuration (the signed-in session), never by a tool argument,
and propose_refund only proposes; a person approves in the bank's system.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .ledger import demo_ledger
from .rag import Index, embedder_from_env, load_corpus
from .tools import CaseTools

ROOT = Path(__file__).resolve().parent.parent


def build(customer: str | None = None, ledger=None) -> tuple[MCPServer, CaseTools]:
    customer = customer or os.getenv("WAKEEL_CUSTOMER", "sara")
    tools = CaseTools(customer, ledger or demo_ledger(), Index(load_corpus(ROOT / "corpus"), embedder_from_env()))
    mcp = MCPServer("wakeel", instructions="Card-dispute tools for a fictional bank. Refunds are proposals only: a bank employee approves them.")

    @mcp.tool(description="Search the bank's dispute, fee and privacy policies; returns passages with section ids to cite.")
    def search_policy(query: str) -> dict:
        return json.loads(tools.call("search_policy", {"query": query}))

    @mcp.tool(description="The signed-in customer's recent card transactions, newest first.")
    def list_transactions(merchant: str | None = None, days: int = 90) -> dict:
        return json.loads(tools.call("list_transactions", {"merchant": merchant, "days": days}))

    @mcp.tool(description="Identical transactions (same merchant and amount within 24 hours); the first is the original.")
    def find_duplicates() -> dict:
        return json.loads(tools.call("find_duplicates", {}))

    @mcp.tool(description="Propose a refund for human approval. Does not move money.")
    def propose_refund(transaction_id: str, amount_sar: float, reason: str, policy_section: str) -> dict:
        return json.loads(tools.call("propose_refund", {"transaction_id": transaction_id, "amount_sar": amount_sar, "reason": reason, "policy_section": policy_section}))

    return mcp, tools


if __name__ == "__main__":
    build()[0].run("stdio")
