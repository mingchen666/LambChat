"""OpenAI-compatible image generation tool for LambChat agents."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import sys
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, InjectedToolArg

from src.infra.logging import get_logger
from src.infra.storage.s3.service import get_or_init_storage
from src.infra.tool.backend_utils import (
    get_base_url_from_runtime,
    get_user_id_from_runtime,
)
from src.kernel.config import settings

try:
    from langchain.tools import ToolRuntime  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    _mod = type(sys)("langchain.tools")
    _mod.ToolRuntime = Any  # type: ignore[attr-defined]
    sys.modules.setdefault("langchain.tools", _mod)
    from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

logger = get_logger(__name__)

DEFAULT_IMAGE_GENERATION_BASE_URL = "https://api.openai.com/v1"
DEFAULT_IMAGE_GENERATION_MODEL = "gpt-image-2"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _strip_data_url_prefix(value: str) -> tuple[str, str]:
    match = re.match(r"^data:([^;]+);base64,(.+)$", value, re.DOTALL)
    if not match:
        return "", value
    return match.group(1), match.group(2)


def _guess_mime(filename: str, fallback: str = "image/png") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def _filename_from_url(url: str, index: int) -> str:
    parsed = urlparse(url)
    name = parsed.path.rstrip("/").split("/")[-1]
    if name:
        return name
    return f"image-{index + 1}.png"


def _resolve_base_url() -> str:
    base_url = (
        getattr(settings, "IMAGE_GENERATION_BASE_URL", "") or DEFAULT_IMAGE_GENERATION_BASE_URL
    )
    return str(base_url).rstrip("/")


def _resolve_model() -> str:
    model = getattr(settings, "IMAGE_GENERATION_MODEL", "") or DEFAULT_IMAGE_GENERATION_MODEL
    return str(model).strip() or DEFAULT_IMAGE_GENERATION_MODEL


async def _download_image_source(
    url: str,
    runtime: ToolRuntime | None,
    *,
    index: int = 0,
) -> tuple[bytes, str, str]:
    resolved = url
    if resolved.startswith("/"):
        base_url = get_base_url_from_runtime(runtime)
        if base_url:
            resolved = f"{base_url}{resolved}"

    if resolved.startswith("data:"):
        mime, data = _strip_data_url_prefix(resolved)
        decoded = base64.b64decode(data)
        ext = (mimetypes.guess_extension(mime) or ".png").lstrip(".")
        return decoded, mime or "image/png", f"inline-image-{index + 1}.{ext}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.get(resolved)
        response.raise_for_status()
        content = response.content
        content_type = response.headers.get("content-type", "") or _guess_mime(resolved)
        filename = _filename_from_url(resolved, index)
        return content, content_type, filename


async def _upload_image_bytes(
    data: bytes,
    *,
    user_id: str,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    storage = await get_or_init_storage()
    result = await storage.upload_bytes(
        data,
        folder=f"generated-images/{user_id}",
        filename=filename,
        content_type=content_type,
    )
    return {
        "key": result.key,
        "url": result.url,
        "size": getattr(result, "size", len(data)),
        "content_type": getattr(result, "content_type", content_type),
    }


def _extract_image_payload(data: dict[str, Any]) -> tuple[bytes, str]:
    if isinstance(data.get("b64_json"), str) and data["b64_json"].strip():
        raw = base64.b64decode(data["b64_json"])
        return raw, "image/png"

    if isinstance(data.get("url"), str) and data["url"].strip():
        parsed = urlparse(data["url"])
        filename = parsed.path.rstrip("/").split("/")[-1] or "image.png"
        mime = _guess_mime(filename)
        return b"", mime

    if isinstance(data.get("base64"), str) and data["base64"].strip():
        raw = base64.b64decode(data["base64"])
        return raw, "image/png"

    if isinstance(data.get("data"), str) and data["data"].strip():
        raw = base64.b64decode(data["data"])
        return raw, "image/png"

    raise ValueError("Image API response did not include a readable image payload")


async def _convert_result_item(
    item: dict[str, Any],
    *,
    user_id: str,
    runtime: ToolRuntime | None,
) -> dict[str, Any]:
    payload = item.get("result") if isinstance(item.get("result"), dict) else item
    if not isinstance(payload, dict):
        raise ValueError("Image API response item is not an object")

    image_bytes, mime = _extract_image_payload(payload)
    if not image_bytes and isinstance(payload.get("url"), str):
        source_bytes, source_mime, filename = await _download_image_source(payload["url"], runtime)
        image_bytes = source_bytes
        mime = source_mime
        if not filename:
            filename = "image.png"
    else:
        filename = f"{payload.get('revised_prompt') or 'image'}.png"

    uploaded = await _upload_image_bytes(
        image_bytes,
        user_id=user_id,
        filename=filename,
        content_type=mime,
    )
    base_url = get_base_url_from_runtime(runtime)
    proxy_url = (
        f"{base_url}/api/upload/file/{uploaded['key']}"
        if base_url
        else f"/api/upload/file/{uploaded['key']}"
    )
    result: dict[str, Any] = {
        "url": proxy_url,
        "key": uploaded["key"],
        "size": uploaded["size"],
        "content_type": uploaded["content_type"],
    }
    if payload.get("revised_prompt"):
        result["revised_prompt"] = payload.get("revised_prompt")
    return result


async def _call_generation_api(
    *,
    prompt: str,
    size: str,
    quality: str,
    n: int,
    output_format: str,
    runtime: ToolRuntime | None,
) -> dict[str, Any]:
    api_key = getattr(settings, "IMAGE_GENERATION_API_KEY", "") or ""
    if not api_key:
        return {"error": "IMAGE_GENERATION_API_KEY is not configured"}

    base_url = _resolve_base_url()
    model = _resolve_model()
    timeout = getattr(settings, "IMAGE_GENERATION_TIMEOUT", 120) or 120
    user_id = get_user_id_from_runtime(runtime) or "anonymous"

    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": n,
        "output_format": output_format,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/images/generations",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

    items = []
    if isinstance(body, dict):
        raw_items = body.get("data")
        if isinstance(raw_items, list):
            items = [item for item in raw_items if isinstance(item, dict)]
        elif isinstance(raw_items, dict):
            items = [raw_items]

    if not items:
        return {
            "error": "Image API did not return any image data",
            "raw_response": body,
        }

    images = []
    for item in items:
        images.append(await _convert_result_item(item, user_id=user_id, runtime=runtime))

    return {
        "success": True,
        "images": images,
    }


@tool
async def image_generate(
    prompt: Annotated[str, "The prompt used to generate the image"],
    size: Annotated[str, "Image size, e.g. 1024x1024 or auto"] = "1024x1024",
    quality: Annotated[str, "Image quality"] = "auto",
    n: Annotated[int, "Number of images to generate"] = 1,
    output_format: Annotated[str, "Output format"] = "png",
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Generate images with an OpenAI-compatible image API."""
    try:
        safe_n = max(1, min(int(n), 4))
        result = await _call_generation_api(
            prompt=prompt,
            size=size,
            quality=quality,
            n=safe_n,
            output_format=output_format,
            runtime=runtime,
        )
        return _json(result)
    except Exception as exc:
        logger.warning("[image_generate] failed: %s", exc)
        return _json({"error": f"Image generation failed: {exc}"})


def get_image_generation_tool() -> BaseTool:
    return image_generate
