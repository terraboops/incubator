"""MCP tools for Telegram communication from within agents."""

from __future__ import annotations

from claude_agent_sdk import tool, create_sdk_mcp_server

from incubator.comms.notifications import NotificationDispatcher


def create_telegram_mcp_server(dispatcher: NotificationDispatcher, idea_id: str):
    """Create an MCP server with Telegram tools scoped to an idea context."""

    @tool(
        "send_telegram",
        "Send a notification message to the human via Telegram",
        {"message": str},
    )
    async def send_telegram(args):
        await dispatcher.notify(f"[{idea_id}] {args['message']}")
        return {"content": [{"type": "text", "text": "Message sent"}]}

    @tool(
        "ask_human",
        "Ask the human a question via Telegram and wait for their response. "
        "Use this for critical decisions that require human input.",
        {"question": str, "options": list},
    )
    async def ask_human(args):
        question = f"*{idea_id}*\n\n{args['question']}"
        options = args.get("options", ["approve", "reject"])
        response = await dispatcher.ask(question, options)
        return {"content": [{"type": "text", "text": f"Human response: {response}"}]}

    @tool(
        "notify_phase_complete",
        "Notify the human that an agent phase has completed",
        {"phase": str, "summary": str},
    )
    async def notify_phase_complete(args):
        msg = f"✅ *{idea_id}* — `{args['phase']}` complete\n\n{args['summary']}"
        await dispatcher.notify(msg)
        return {"content": [{"type": "text", "text": "Phase completion notified"}]}

    return create_sdk_mcp_server(
        "telegram-tools",
        tools=[send_telegram, ask_human, notify_phase_complete],
    )
