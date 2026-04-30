"""NVIDIA NIM client for OpenAI-compatible chat completions."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "qwen/qwen3.5-122b-a10b"


class NimClientError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class NimClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 60
    temperature: float = 0.5
    max_tokens: int = 700

    def chat_completion(self, *, system_prompt: str, user_prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(exc)
            raise NimClientError(f"HTTP {exc.code} from NIM: {body}") from exc
        except error.URLError as exc:
            raise NimClientError(f"NIM request failed: {exc}") from exc

        try:
            response: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NimClientError(f"NIM returned invalid JSON: {raw[:500]}") from exc

        choices = response.get("choices") or []
        if not choices:
            raise NimClientError(f"NIM response has no choices: {response}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        parts.append(str(text))
                elif item:
                    parts.append(str(item))
            return "".join(parts).strip()
        raise NimClientError(f"NIM response has unexpected message content: {message}")


@lru_cache(maxsize=1)
def get_nim_client() -> NimClient | None:
    settings = load_nim_settings()
    api_key = (
        settings.get("NIM_API_KEY")
        or settings.get("NVIDIA_API_KEY")
        or settings.get("NGC_API_KEY")
    )
    if not api_key:
        return None
    base_url = settings.get("NIM_BASE_URL", DEFAULT_BASE_URL)
    model = settings.get("NIM_MODEL", DEFAULT_MODEL)
    timeout_seconds = int(float(settings.get("NIM_TIMEOUT_SECONDS", "60")))
    temperature = float(settings.get("NIM_TEMPERATURE", "0.5"))
    max_tokens = int(float(settings.get("NIM_MAX_TOKENS", "700")))
    return NimClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        max_tokens=max_tokens,
    )