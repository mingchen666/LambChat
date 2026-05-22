from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import deps as api_deps
from src.api.routes import mcp as mcp_route
from src.kernel.schemas.mcp import MCPServerResponse, MCPToolInfo, MCPTransport
from src.kernel.schemas.user import TokenPayload


def _fake_user() -> TokenPayload:
    return TokenPayload(
        sub="user-1",
        username="tester",
        roles=["user"],
        permissions=["mcp:read"],
    )


def _fake_admin() -> TokenPayload:
    return TokenPayload(
        sub="admin-1",
        username="admin",
        roles=["admin"],
        permissions=["mcp:read", "mcp:admin"],
    )


@pytest.mark.asyncio
async def test_list_mcp_servers_returns_paginated_response() -> None:
    class _FakeStorage:
        async def get_visible_servers(self, user_id: str, is_admin: bool, user_roles):
            assert user_id == "user-1"
            assert is_admin is False
            assert user_roles == ["user"]
            return [
                MCPServerResponse(
                    name=f"server-{i}",
                    transport=MCPTransport.SSE,
                    enabled=True,
                    url=f"https://example.com/{i}",
                    is_system=False,
                    can_edit=True,
                )
                for i in range(5)
            ]

    app = FastAPI()
    app.include_router(mcp_route.router, prefix="/api/mcp")
    app.dependency_overrides[api_deps.get_current_user_required] = _fake_user
    app.dependency_overrides[mcp_route.get_mcp_storage] = lambda: _FakeStorage()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/mcp/?skip=2&limit=2&q=server")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 5
    assert payload["skip"] == 2
    assert payload["limit"] == 2
    assert [server["name"] for server in payload["servers"]] == ["server-2", "server-3"]


@pytest.mark.asyncio
async def test_admin_mcp_list_includes_internal_server() -> None:
    class _FakeStorage:
        async def get_visible_servers(self, user_id: str, is_admin: bool, user_roles):
            assert is_admin is True
            return []

    app = FastAPI()
    app.include_router(mcp_route.admin_router, prefix="/api/admin/mcp")
    app.dependency_overrides[api_deps.get_current_user_required] = _fake_admin
    app.dependency_overrides[mcp_route.get_mcp_storage] = lambda: _FakeStorage()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/admin/mcp/")

    assert response.status_code == 200
    payload = response.json()
    assert any(server["name"] == "lambchat_internal" for server in payload["servers"])


@pytest.mark.asyncio
async def test_admin_internal_tool_discovery_uses_internal_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_internal_tool_infos(*args, **kwargs):
        return [
            MCPToolInfo(
                name="image_generate",
                description="Generate images",
                parameters=[],
                policy_configured=True,
            )
        ]

    monkeypatch.setattr(
        "src.infra.tool.internal_registry.get_internal_tool_infos",
        fake_get_internal_tool_infos,
    )

    app = FastAPI()
    app.include_router(mcp_route.admin_router, prefix="/api/admin/mcp")
    app.dependency_overrides[api_deps.get_current_user_required] = _fake_admin
    app.dependency_overrides[mcp_route.get_mcp_storage] = lambda: object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/admin/mcp/lambchat_internal/tools")

    assert response.status_code == 200
    payload = response.json()
    assert payload["server_name"] == "lambchat_internal"
    assert payload["count"] == 1
    assert payload["tools"][0]["name"] == "image_generate"
