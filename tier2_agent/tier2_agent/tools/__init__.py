"""Implementation thật của 4 tool (TIER2_DESIGN.md mục 2).

Mỗi module ở đây export 1 factory `make_..._tool(...)` trả về callable
`(tool_input: dict) -> ToolResult` để tiêm vào `ToolRegistry` — tách khỏi
`tool_registry.py` (chỉ định tuyến + validate) theo đúng ranh giới thiết kế.
"""
