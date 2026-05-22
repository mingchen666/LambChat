import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class _Runtime:
    def __init__(self, user_id: str | None, base_url: str = "https://app.example.com") -> None:
        context = SimpleNamespace(user_id=user_id) if user_id is not None else None
        self.config = {"configurable": {"context": context, "base_url": base_url}}


def _load_module_from_path(module_name: str, relative_path: str):
    path = Path(__file__).parents[3] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stub_context_tool_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    def tool(name: str):
        return SimpleNamespace(name=name)

    monkeypatch.setitem(
        sys.modules,
        "src.infra.tool.human_tool",
        SimpleNamespace(get_human_tool=lambda session_id=None: tool("ask_human")),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.infra.tool.reveal_file_tool",
        SimpleNamespace(get_reveal_file_tool=lambda: tool("reveal_file")),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.infra.tool.reveal_project_tool",
        SimpleNamespace(get_reveal_project_tool=lambda: tool("reveal_project")),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.infra.tool.transfer_file_tool",
        SimpleNamespace(
            get_transfer_file_tool=lambda: tool("transfer_file"),
            get_transfer_path_tool=lambda: tool("transfer_path"),
        ),
    )


def test_get_image_generation_tool_returns_expected_tool() -> None:
    from src.infra.tool.image_generation_tool import get_image_generation_tool

    tool = get_image_generation_tool()

    assert tool.name == "image_generate"


@pytest.mark.asyncio
async def test_image_generate_calls_images_api_and_uploads_base64_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.tool import image_generation_tool

    captured: dict[str, object] = {}
    image_bytes = b"fake-png"
    b64_image = base64.b64encode(image_bytes).decode("ascii")

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, request_url: str, **kwargs):
            captured["request_url"] = request_url
            captured["kwargs"] = kwargs
            return _FakeResponse({"data": [{"b64_json": b64_image, "revised_prompt": "a cat"}]})

    class _FakeStorage:
        is_local = False

        async def upload_bytes(self, data: bytes, folder: str, filename: str, content_type: str):
            captured["upload"] = {
                "data": data,
                "folder": folder,
                "filename": filename,
                "content_type": content_type,
            }
            return SimpleNamespace(
                key=f"{folder}/{filename}",
                url="https://oss.example.com/generated-images%2Fuser-1%2Fcat.png?Signature=secret",
            )

    async def fake_get_or_init_storage():
        return _FakeStorage()

    monkeypatch.setattr(
        image_generation_tool.httpx, "AsyncClient", lambda **kwargs: _FakeHttpClient()
    )
    monkeypatch.setattr(image_generation_tool, "get_or_init_storage", fake_get_or_init_storage)
    monkeypatch.setattr(image_generation_tool.settings, "IMAGE_GENERATION_API_KEY", "sk-test")
    monkeypatch.setattr(
        image_generation_tool.settings,
        "IMAGE_GENERATION_BASE_URL",
        "https://api.example.com/v1",
    )
    monkeypatch.setattr(image_generation_tool.settings, "IMAGE_GENERATION_MODEL", "gpt-image-2")
    monkeypatch.setattr(image_generation_tool.settings, "IMAGE_GENERATION_TIMEOUT", 123)

    result = json.loads(
        await image_generation_tool.image_generate.coroutine(
            prompt="draw a cat",
            size="1024x1024",
            quality="high",
            n=1,
            output_format="png",
            runtime=_Runtime("user-1"),
        )
    )

    assert result["success"] is True
    assert set(result.keys()) == {"success", "images"}
    assert result["images"][0] == {
        "url": "https://app.example.com/api/upload/file/generated-images/user-1/a cat.png",
        "key": "generated-images/user-1/a cat.png",
        "content_type": "image/png",
        "revised_prompt": "a cat",
    }
    assert result["images"][0]["revised_prompt"] == "a cat"
    assert captured["request_url"] == "https://api.example.com/v1/images/generations"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["kwargs"]["json"] == {
        "model": "gpt-image-2",
        "prompt": "draw a cat",
        "size": "1024x1024",
        "quality": "high",
        "n": 1,
        "output_format": "png",
    }
    assert captured["upload"]["data"] == image_bytes
    assert captured["upload"]["folder"] == "generated-images/user-1"
    assert captured["upload"]["content_type"] == "image/png"


def test_image_generate_schema_is_text_to_image_only() -> None:
    from src.infra.tool.image_generation_tool import get_image_generation_tool

    tool = get_image_generation_tool()
    fields = set(tool.args_schema.model_fields)

    assert "input_images" not in fields
    assert "mask_url" not in fields


@pytest.mark.asyncio
async def test_image_generate_returns_error_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.tool import image_generation_tool

    monkeypatch.setattr(image_generation_tool.settings, "IMAGE_GENERATION_API_KEY", "")

    result = json.loads(
        await image_generation_tool.image_generate.coroutine(
            prompt="draw a cat",
            runtime=_Runtime("user-1"),
        )
    )

    assert result == {"error": "IMAGE_GENERATION_API_KEY is not configured"}


@pytest.mark.asyncio
async def test_search_agent_context_includes_image_generation_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_context_tool_imports(monkeypatch)
    search_context = _load_module_from_path(
        "search_context_with_image_tool_under_test",
        "src/agents/search_agent/context.py",
    )

    monkeypatch.setattr(search_context.settings, "ENABLE_IMAGE_GENERATION", True)
    monkeypatch.setattr(search_context.settings, "ENABLE_AUDIO_TRANSCRIPTION", False)
    monkeypatch.setattr(search_context.settings, "ENABLE_MEMORY", False)
    monkeypatch.setattr(search_context.settings, "ENABLE_SANDBOX", False)
    monkeypatch.setattr(search_context.settings, "ENABLE_SKILLS", False)

    ctx = search_context.SearchAgentContext(user_id="user-1")
    await ctx.setup()

    names = {tool.name for tool in ctx.tools}
    assert "image_generate" in names


@pytest.mark.asyncio
async def test_fast_agent_context_includes_image_generation_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_context_tool_imports(monkeypatch)
    fast_context = _load_module_from_path(
        "fast_context_with_image_tool_under_test",
        "src/agents/fast_agent/context.py",
    )

    monkeypatch.setattr(fast_context.settings, "ENABLE_IMAGE_GENERATION", True)
    monkeypatch.setattr(fast_context.settings, "ENABLE_AUDIO_TRANSCRIPTION", False)
    monkeypatch.setattr(fast_context.settings, "ENABLE_MEMORY", False)
    monkeypatch.setattr(fast_context.settings, "ENABLE_SANDBOX", False)
    monkeypatch.setattr(fast_context.settings, "ENABLE_SKILLS", False)

    ctx = fast_context.FastAgentContext(user_id="user-1")
    await ctx.setup()

    names = {tool.name for tool in ctx.tools}
    assert "image_generate" in names
