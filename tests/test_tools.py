"""Tests for MCP tool registration and basic tool execution."""

import pytest

from nexus.server.state import _call_timestamps


@pytest.fixture(autouse=True)
def reset_rate_limit():
    """Clear rate limit between tests."""
    _call_timestamps.clear()
    yield
    _call_timestamps.clear()


class TestToolRegistration:
    def test_all_tools_registered(self):
        from nexus.server.mcp import mcp

        tools = mcp._tool_manager._tools
        expected = {
            "nexus_scan",
            "nexus_read",
            "nexus_start",
            "nexus_retrieve",
            "nexus_symbols",
            "nexus_register_edit",
            "nexus_remember",
            "nexus_stats",
            "nexus_rename",
            "nexus_analytics",
            "nexus_enrich",
            "nexus_cross_project",
        }
        assert set(tools.keys()) == expected

    def test_tool_count(self):
        from nexus.server.mcp import mcp

        assert len(mcp._tool_manager._tools) == 12


class TestNexusScan:
    def test_scan_sample_project(self, sample_project):
        from nexus.server.tools_index import register
        from mcp.server.fastmcp import FastMCP

        test_mcp = FastMCP("test")
        register(test_mcp)

        # Call the tool function directly
        scan_fn = test_mcp._tool_manager._tools["nexus_scan"]
        result = scan_fn.fn(project=str(sample_project), languages="python")

        assert "Scanned:" in result
        assert "Files:" in result
        assert "Symbols:" in result

    def test_scan_nonexistent_project(self):
        from nexus.server.tools_index import register
        from mcp.server.fastmcp import FastMCP

        test_mcp = FastMCP("test")
        register(test_mcp)

        scan_fn = test_mcp._tool_manager._tools["nexus_scan"]
        with pytest.raises(ValueError, match="not found"):
            scan_fn.fn(project="/nonexistent/path", languages="python")


class TestNexusRemember:
    def test_remember_validates_type(self, sample_project):
        from nexus.server.state import activate_project
        activate_project(str(sample_project))

        from nexus.server.tools_refactor import register
        from mcp.server.fastmcp import FastMCP

        test_mcp = FastMCP("test")
        register(test_mcp)

        remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
        result = remember_fn.fn(content="test", type="invalid_type")
        assert "Invalid type" in result

    def test_remember_validates_length(self, sample_project):
        from nexus.server.state import activate_project
        activate_project(str(sample_project))

        from nexus.server.tools_refactor import register
        from mcp.server.fastmcp import FastMCP

        test_mcp = FastMCP("test")
        register(test_mcp)

        remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
        long_content = " ".join(["word"] * 25)
        result = remember_fn.fn(content=long_content, type="decision")
        assert "too long" in result

    def test_remember_valid(self, sample_project):
        from nexus.server.state import activate_project
        activate_project(str(sample_project))

        from nexus.server.tools_refactor import register
        from mcp.server.fastmcp import FastMCP

        test_mcp = FastMCP("test")
        register(test_mcp)

        remember_fn = test_mcp._tool_manager._tools["nexus_remember"]
        result = remember_fn.fn(content="Use SQLite WAL mode", type="decision")
        assert "Remembered [decision]" in result
