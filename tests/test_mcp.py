import asyncio
import json

from wakeel.mcp_server import build


def _payload(result):
    """call_tool returns content blocks and/or structured output, depending on version."""
    if isinstance(result, tuple):
        result = result[-1] if isinstance(result[-1], dict) else result[0]
    if isinstance(result, dict):
        return result.get("result", result)
    if hasattr(result, "structuredContent") and result.structuredContent:
        return result.structuredContent
    blocks = getattr(result, "content", result)
    return json.loads(blocks[0].text)


def test_mcp_tools_are_listed_and_bound_to_the_customer():
    mcp, tools = build("sara")

    async def go():
        names = {t.name for t in await mcp.list_tools()}
        assert names == {"search_policy", "list_transactions", "find_duplicates", "propose_refund"}
        dup = _payload(await mcp.call_tool("find_duplicates", {}))
        assert any(g["original"] == "tx_1001" for g in dup["groups"])
        other = _payload(await mcp.call_tool("propose_refund", {"transaction_id": "tx_2002", "amount_sar": 4599, "reason": "duplicate_charge", "policy_section": "disputes#3"}))
        assert "no such transaction" in other["error"]

    asyncio.run(go())
    assert tools.proposal is None
