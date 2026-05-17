"""LLM client for OpenAI-compatible chat completions with BYOK support.

Default: qwen/qwen3.5-122b-a10b via NVIDIA NIM.
Bring-your-own-key: GPT, Claude, Gemini, or any OpenAI-compatible API.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error, request

log = logging.getLogger("hh_auto.llm")

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "mistralai/mistral-medium-3.5-128b"

# Best-effort header asking providers not to use data for training.
# Supported by some providers (e.g. Anthropic, OpenAI org-level).
# NVIDIA NIM may ignore it, but we send it as a signal.
DO_NOT_TRAIN_HEADER = ("X-Do-Not-Train", "true")


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120
    temperature: float = 0.5
    max_tokens: int = 700
    do_not_train: bool = True

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.do_not_train:
            h["x-do-not-train"] = "true"
            h["X-Do-Not-Train"] = "true"
        return h

    def chat_completion(self, *, system_prompt: str, user_prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    raw = resp.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise LLMError(f"HTTP {exc.code} from LLM: {body}") from exc
            except Exception as exc:
                last_exc = exc
                log.warning("LLM request attempt %d/%d failed: %s", attempt + 1, 3, exc)
                if attempt < 2:
                    import time
                    time.sleep(3)
        else:
            raise LLMError(f"LLM request failed after 3 attempts: {last_exc}") from last_exc

        try:
            response = json.loads(raw)
        except Exception as exc:
            raise LLMError(f"LLM returned invalid JSON: {raw[:500]}") from exc

        choices = response.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMError(f"LLM response has no choices: {response}")

        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = message.get("content")
            if content is not None:
                return str(content)
            reasoning = message.get("reasoning_content")
            if reasoning is not None:
                return str(reasoning)
            raise LLMError(f"LLM response has unexpected message content: {message}")

        if isinstance(message, str):
            return message

        raise LLMError(f"LLM response has unexpected message content: {message}")


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    data: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        data[key] = value
    return data


def _candidate_env_files() -> list[Path]:
    candidates: list[Path] = []
    custom = os.environ.get("NIM_ENV_FILE")
    if custom:
        candidates.append(Path(custom).expanduser())
    candidates.append(Path(".env"))
    candidates.append(Path(".env.local"))
    return candidates


@lru_cache(maxsize=1)
def load_nim_settings() -> dict[str, str]:
    settings = dict(os.environ)
    for candidate in _candidate_env_files():
        for key, value in _parse_env_file(candidate).items():
            settings.setdefault(key, value)
    return settings


def get_client_from_settings(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    do_not_train: bool = True,
) -> LLMClient | None:
    settings = load_nim_settings()

    key = api_key
    if not key:
        key = (
            settings.get("LLM_API_KEY")
            or settings.get("OPENAI_API_KEY")
            or settings.get("NIM_API_KEY")
            or settings.get("NVIDIA_API_KEY")
            or settings.get("NGC_API_KEY")
        )
    if not key:
        return None

    url = base_url or settings.get("LLM_BASE_URL") or settings.get("NIM_BASE_URL") or DEFAULT_BASE_URL
    mdl = model or settings.get("LLM_MODEL") or settings.get("NIM_MODEL") or DEFAULT_MODEL

    temperature = float(settings.get("NIM_TEMPERATURE", "0.5"))
    max_tokens = int(float(settings.get("NIM_MAX_TOKENS", "700")))
    timeout_seconds = int(float(settings.get("NIM_TIMEOUT_SECONDS", "60")))

    return LLMClient(
        api_key=key,
        base_url=url,
        model=mdl,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        max_tokens=max_tokens,
        do_not_train=do_not_train,
    )


# Backward-compatible alias
def get_nim_client() -> LLMClient | None:
    return get_client_from_settings()
