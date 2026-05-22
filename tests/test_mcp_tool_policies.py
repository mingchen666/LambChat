from types import SimpleNamespace

import pytest


def test_mcp_tool_policy_schema_preserves_allowed_roles_and_quotas() -> None:
    from src.kernel.schemas.mcp import MCPRoleQuota, MCPToolPolicy

    policy = MCPToolPolicy.model_validate(
        {
            "allowed_roles": ["admin", "user"],
            "role_quotas": {
                "admin": {"daily_limit": 3, "weekly_limit": 10},
            },
        }
    )

    assert policy.allowed_roles == ["admin", "user"]
    assert policy.role_quotas == {"admin": MCPRoleQuota(daily_limit=3, weekly_limit=10)}


@pytest.mark.asyncio
async def test_mcp_storage_round_trips_tool_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infra.mcp.storage import MCPStorage
    from src.kernel.schemas.mcp import MCPRoleQuota

    fake_docs: dict[tuple[str, str], dict[str, object]] = {}

    class _FakeCollection:
        async def update_one(self, query, update, upsert=False):
            key = (query["server_name"], query["tool_name"])
            fake_docs[key] = update["$set"]
            return SimpleNamespace()

        async def find_one(self, query):
            return fake_docs.get((query["server_name"], query["tool_name"]))

    storage = MCPStorage()
    monkeypatch.setattr(storage, "_get_tool_policies_collection", lambda: _FakeCollection())
    monkeypatch.setattr(storage, "_invalidate_all_cache", lambda: None)
    monkeypatch.setattr(storage, "_invalidate_user_cache", lambda user_id: None)

    await storage.set_tool_policy(
        server_name="lambchat_internal",
        tool_name="image_generate",
        allowed_roles=["admin"],
        role_quotas={"admin": MCPRoleQuota(daily_limit=2)},
        updated_by="admin-1",
    )

    policy = await storage.get_tool_policy("lambchat_internal", "image_generate")

    assert policy is not None
    assert policy.allowed_roles == ["admin"]
    assert policy.role_quotas["admin"].daily_limit == 2


@pytest.mark.asyncio
async def test_internal_tool_policies_filter_blocked_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.tools import BaseTool

    from src.infra.tool import internal_registry
    from src.kernel.schemas.mcp import MCPToolPolicy

    class _FakeTool(BaseTool):
        name: str
        description: str = ""

        def _run(self, *args, **kwargs):
            return "sync"

        async def _arun(self, *args, **kwargs):
            return "async"

    class _FakeStorage:
        async def list_tool_policies(self, server_name: str):
            assert server_name == "lambchat_internal"
            return {
                "image_generate": MCPToolPolicy(
                    server_name="lambchat_internal",
                    tool_name="image_generate",
                    allowed_roles=["admin"],
                )
            }

    monkeypatch.setattr(internal_registry, "MCPStorage", lambda: _FakeStorage())
    monkeypatch.setattr(
        internal_registry,
        "build_internal_tools",
        lambda: [_FakeTool(name="image_generate"), _FakeTool(name="env_var_list")],
    )

    tools = await internal_registry.get_internal_tools_for_user(
        user_id="user-1",
        user_roles=["user"],
        is_admin=False,
    )

    assert [tool.name for tool in tools] == ["env_var_list"]
