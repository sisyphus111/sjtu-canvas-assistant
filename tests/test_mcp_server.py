from sjtu_canvas_assistant.mcp_server import MCPStdioServer, ToolDefinition, _schema


def _server() -> MCPStdioServer:
    tool = ToolDefinition(
        name="echo",
        title="Echo",
        description="Echo arguments",
        input_schema=_schema({"text": {"type": "string"}}, required=["text"]),
        handler=lambda args: {"echo": args["text"]},
    )
    return MCPStdioServer({"echo": tool})


def test_initialize_response_declares_tools_capability() -> None:
    response = _server().handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in response["result"]["capabilities"]


def test_tools_list_returns_input_schema() -> None:
    response = _server().handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )

    assert response is not None
    tools = response["result"]["tools"]
    assert tools[0]["name"] == "echo"
    assert tools[0]["inputSchema"]["required"] == ["text"]


def test_tools_call_returns_text_and_structured_content() -> None:
    response = _server().handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello"}},
        }
    )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"echo": "hello"}
    assert '"echo": "hello"' in result["content"][0]["text"]


def test_unknown_tool_is_invalid_params_error() -> None:
    server = _server()

    try:
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            }
        )
    except ValueError as exc:
        assert "Unknown tool" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_initialized_notification_has_no_response() -> None:
    assert (
        _server().handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is None
    )
