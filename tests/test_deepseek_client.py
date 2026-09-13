"""CS-2 tests for the bounded and redacted DeepSeek client."""

import json
from typing import Any

import pytest

from xianyu_assistant.customer_service.deepseek_client import DeepSeekClient, DeepSeekClientError
from xianyu_assistant.customer_service.models import DeepSeekSettings, ImagePayload
from xianyu_assistant.security.credential_store import MemoryCredentialStore


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload
        self.closed = False

    def read(self) -> bytes:
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")

    def close(self) -> None:
        self.closed = True


def _store() -> MemoryCredentialStore:
    store = MemoryCredentialStore()
    store.set("deepseek_api_key", "test-secret")
    return store


def test_text_request_uses_configured_endpoint_and_parses_response() -> None:
    captured: list[Any] = []

    def opener(request: Any, **kwargs: object) -> _FakeResponse:
        captured.append((request, kwargs))
        return _FakeResponse(
            200,
            {"id": "request-1", "model": "text-model", "choices": [{"message": {"content": "OK"}}]},
        )

    response = DeepSeekClient(
        DeepSeekSettings(
            base_url="https://example.test/v1",
            text_model="text-model",
            vision_model="vision-model",
        ),
        _store(),
        opener=opener,
    ).complete_text(system_prompt="system", user_prompt="user")

    request, kwargs = captured[0]
    assert request.full_url == "https://example.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert kwargs["timeout"] == 20.0
    assert response.text == "OK"
    assert response.request_id == "request-1"


def test_vision_request_contains_in_memory_base64_image_and_no_local_path() -> None:
    captured: list[dict[str, object]] = []

    def opener(request: Any, **_kwargs: object) -> _FakeResponse:
        captured.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(200, {"choices": [{"message": {"content": "看到了"}}]})

    response = DeepSeekClient(DeepSeekSettings(), _store(), opener=opener).complete_with_image(
        system_prompt="system",
        user_prompt="describe",
        image=ImagePayload(message_key="message-1", content=b"image-bytes", mime_type="image/png"),
    )

    user_content = captured[0]["messages"][1]["content"]  # type: ignore[index]
    assert response.text == "看到了"
    assert "data:image/png;base64," in str(user_content)
    assert "message-1" not in str(user_content)


def test_retry_is_bounded_and_uses_exponential_backoff_for_server_failures() -> None:
    responses = iter(
        [
            _FakeResponse(503, {"error": "temporary"}),
            _FakeResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
        ]
    )
    sleeps: list[float] = []

    client = DeepSeekClient(
        DeepSeekSettings(),
        _store(),
        max_attempts=2,
        backoff_seconds=0.1,
        opener=lambda *_args, **_kwargs: next(responses),
        sleeper=sleeps.append,
    )

    assert client.test_connection().text == "OK"
    assert sleeps == [0.1]


def test_invalid_response_error_does_not_echo_api_key_or_response_body() -> None:
    secret = "test-secret"
    client = DeepSeekClient(
        DeepSeekSettings(),
        _store(),
        max_attempts=1,
        opener=lambda *_args, **_kwargs: _FakeResponse(200, f"invalid {secret}"),
    )

    with pytest.raises(DeepSeekClientError) as error:
        client.test_connection()

    assert secret not in str(error.value)
    assert "invalid" not in str(error.value)
