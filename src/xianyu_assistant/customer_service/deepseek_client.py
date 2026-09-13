"""Small, dependency-light DeepSeek client with bounded retries and redacted errors."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xianyu_assistant.customer_service.models import DeepSeekSettings, ImagePayload, ModelResponse
from xianyu_assistant.security.credential_store import (
    CredentialStore,
    CredentialStoreUnavailableError,
)


class DeepSeekClientError(RuntimeError):
    """A safe-to-display client error that never includes the API key or body."""


class DeepSeekConfigurationError(DeepSeekClientError):
    """Raised when endpoint or secure credentials are not ready."""


class DeepSeekClient:
    """Call DeepSeek chat completions using only sanitized caller context."""

    _credential_name = "deepseek_api_key"

    def __init__(
        self,
        settings: DeepSeekSettings,
        credential_store: CredentialStore,
        *,
        timeout_seconds: float = 20.0,
        max_attempts: int = 2,
        backoff_seconds: float = 0.25,
        opener: Callable[..., HTTPResponse] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or max_attempts <= 0 or backoff_seconds < 0:
            raise ValueError("DeepSeek 客户端的超时、重试次数和退避参数无效。")
        self._settings = settings
        self._credential_store = credential_store
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._opener = opener
        self._sleeper = sleeper

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
    ) -> ModelResponse:
        """Send a text request using the configured or explicitly supplied model."""
        return self._complete(
            model=model or self._settings.text_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

    def complete_with_image(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image: ImagePayload,
        model: str | None = None,
    ) -> ModelResponse:
        """Send a base64 data URL from an in-memory customer image."""
        encoded = base64.b64encode(image.content).decode("ascii")
        return self._complete(
            model=model or self._settings.vision_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
        )

    def test_connection(self) -> ModelResponse:
        """Send a fixed non-customer probe when the user explicitly requests it."""
        return self.complete_text(
            system_prompt="你是连接测试助手。只需返回 OK。",
            user_prompt="请回复 OK。不要输出其他内容。",
        )

    def _complete(self, *, model: str, messages: list[dict[str, object]]) -> ModelResponse:
        api_key = self._read_api_key()
        body = json.dumps({"model": model, "messages": messages}, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._settings.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        last_error: DeepSeekClientError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._send(request)
            except DeepSeekClientError as error:
                last_error = error
                if attempt == self._max_attempts or not error.args or not str(error).startswith("retry:"):
                    break
                self._sleeper(self._backoff_seconds * (2 ** (attempt - 1)))
        if last_error is None:
            raise DeepSeekClientError("DeepSeek 请求失败。")
        raise DeepSeekClientError(str(last_error).removeprefix("retry: ").strip()) from last_error

    def _send(self, request: Request) -> ModelResponse:
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
            status = int(getattr(response, "status", 200))
            raw_body = response.read()
        except HTTPError as error:
            if error.code == 429 or error.code >= 500:
                raise DeepSeekClientError(f"retry: DeepSeek 服务暂时不可用（HTTP {error.code}）。") from error
            raise DeepSeekClientError(f"DeepSeek 请求被拒绝（HTTP {error.code}）。") from error
        except (TimeoutError, URLError, OSError) as error:
            raise DeepSeekClientError("retry: DeepSeek 网络请求失败或超时。") from error
        finally:
            if "response" in locals() and hasattr(response, "close"):
                response.close()

        if status == 429 or status >= 500:
            raise DeepSeekClientError(f"retry: DeepSeek 服务暂时不可用（HTTP {status}）。")
        if status >= 400:
            raise DeepSeekClientError(f"DeepSeek 请求被拒绝（HTTP {status}）。")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise DeepSeekClientError("DeepSeek 返回格式无效。") from error
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekClientError("DeepSeek 返回了空内容。")
        return ModelResponse(
            text=content,
            model=payload.get("model"),
            request_id=payload.get("id"),
        )

    def _read_api_key(self) -> str:
        if not self._credential_store.available:
            raise DeepSeekConfigurationError("安全凭据存储不可用，不能启用 DeepSeek。")
        try:
            api_key = self._credential_store.get(self._credential_name)
        except CredentialStoreUnavailableError as error:
            raise DeepSeekConfigurationError("读取 DeepSeek 安全凭据失败。") from error
        if not api_key:
            raise DeepSeekConfigurationError("尚未配置 DeepSeek API Key。")
        return api_key
