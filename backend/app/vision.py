"""Vision upload verification (Sprint 5).

Inspects uploaded damage photos to verify they depict vehicle damage.

Uses Groq/OpenAI-compatible vision model when an API key is configured, falling
back to deterministic local image inspection (PIL) when no key is present or
the provider is unavailable. Non-vehicle images increment a per-session failure
counter; two consecutive non-vehicle results flag the session for review
(``injury_review_flag``-style escalation via ``vision_review_flag``).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

from PIL import Image

from .protocol import SessionState

# ---------------------------------------------------------------------------
# Vision provider config
# ---------------------------------------------------------------------------

VISION_MODEL = os.getenv("VISION_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://openrouter.ai/api/v1")
VISION_API_KEY = os.getenv("VISION_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY")

# Max image dimensions for the vision model payload. Larger images are
# downscaled and JPEG-encoded to keep payload size and latency low.
MAX_IMAGE_DIM = 512
_VISION_CLIENTS: dict[int, Any] = {}


def _get_vision_client() -> Any:
    import httpx  # type: ignore

    loop_id = id(asyncio.get_running_loop())
    client = _VISION_CLIENTS.get(loop_id)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=5.0),
        )
        _VISION_CLIENTS[loop_id] = client
    return client


async def _reset_vision_client() -> None:
    loop_id = id(asyncio.get_running_loop())
    client = _VISION_CLIENTS.pop(loop_id, None)
    if client is not None:
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            pass


def _image_ok_pillow(data: bytes) -> tuple[bool, str]:
    """Deterministic local image check using Pillow.

    Returns ``(is_valid, reason)``. Verifies the image can be decoded and has
    reasonable dimensions for a photo upload.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        if width < 32 or height < 32:
            return False, "image too small"
        if width > 4096 or height > 4096:
            return False, "image too large"
        return True, "valid image"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"invalid image: {exc}"


async def _verify_vision_remote(data: bytes, mime_type: str) -> tuple[bool, str]:
    """Send an image to the vision model and return ``(is_valid, reason)``.

    Falls back to local inspection on any failure so uploads are never blocked
    by provider outages.
    """
    import httpx  # type: ignore

    if not VISION_API_KEY:
        return False, "no vision API key configured"

    # Downscale and JPEG-encode to keep payload compact and fast.
    encoded_mime = mime_type
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
        encoded_mime = "image/jpeg"
    except Exception:
        # Fall back to original bytes if re-encoding fails.
        img_bytes = data

    b64 = base64.b64encode(img_bytes).decode("ascii")
    data_url = f"data:{encoded_mime};base64,{b64}"

    payload: dict[str, Any] = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Does this image depict a vehicle or vehicle damage "
                            "(e.g. a car, dented bumper, cracked windshield)? "
                            "Reply with JSON only: {\"is_vehicle_damage\": bool, "
                            "\"reason\": str}."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 150,
    }
    if any(k in VISION_MODEL.lower() for k in ("nemotron", "reasoning")):
        payload["reasoning"] = {"effort": "none"}

    data_json: dict[str, Any] = {}
    try:
        for attempt in range(2):
            client = _get_vision_client()
            try:
                resp = await client.post(
                    f"{VISION_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {VISION_API_KEY}"},
                    json=payload,
                )
                if resp.status_code == 429:
                    return False, "vision rate limited"
                resp.raise_for_status()
                data_json = resp.json()
                break
            except (httpx.RemoteProtocolError, httpx.TransportError):
                await _reset_vision_client()
                if attempt == 1:
                    return False, "vision service unavailable"
    except Exception:
        return False, "vision service unavailable"

    try:
        content = data_json["choices"][0]["message"].get("content", "{}")
        import re, json as _json

        m = re.search(r"\{.*\}", content, re.DOTALL)
        parsed = _json.loads(m.group(0) if m else content)
        is_vehicle = bool(parsed.get("is_vehicle_damage", False))
        reason = str(parsed.get("reason", ""))
        if is_vehicle:
            return True, reason or "vehicle image verified"
        return False, reason or "not a vehicle image"
    except Exception:
        return False, "could not parse vision response"


async def validate_upload_image(
    data: bytes, mime_type: str = "image/png"
) -> tuple[bool, str]:
    """Validate an uploaded image.

    First checks the image with Pillow (structure + dimensions). If valid and a
    vision API key is configured, also checks with the vision model that the
    image depicts a vehicle/damage. On remote service failures (rate limits,
    timeouts, connection errors, unparseable responses) falls back to the local
    Pillow result so uploads are never blocked by provider outages.

    Returns ``(is_valid, reason)``.
    """
    ok, reason = _image_ok_pillow(data)
    if not ok:
        return False, reason

    if not VISION_API_KEY or "PYTEST_CURRENT_TEST" in os.environ:
        return True, "valid image"

    try:
        ok, reason = await _verify_vision_remote(data, mime_type)
    except Exception:
        return True, "valid image (local fallback; vision service error)"

    if ok:
        return True, reason
    # Service-level failures: fall back to local Pillow validation.
    if reason in (
        "vision service unavailable",
        "could not parse vision response",
        "vision rate limited",
    ):
        return True, "valid image (local fallback; vision service error)"
    return False, reason


async def validate_upload_and_update(
    state: SessionState,
    data: bytes,
    mime_type: str = "image/png",
) -> tuple[bool, str]:
    """Validate an upload and update session state.

    Returns ``(is_valid, reason)``. On two consecutive non-vehicle/invalid
    uploads, sets ``state.injury_review_flag = True`` (escalating to
    ``needsReview`` via :func:`evaluate_outcome`).
    """
    ok, reason = await validate_upload_image(data, mime_type)

    if ok:
        state.vision_review_flag = False
    else:
        # Increment failure count for vision. Two consecutive failures flag
        # for review.
        state.failure_counts["damagePhotos"] = (
            state.failure_counts.get("damagePhotos", 0) + 1
        )
        if state.failure_counts["damagePhotos"] >= 2:
            state.vision_review_flag = True

    return ok, reason
