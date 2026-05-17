"""Telegram notifications via Bot API."""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import json

log = logging.getLogger("hh_auto.notifications")

TELEGRAM_API = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str
    proxy: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TelegramConfig | None":
        if not d:
            return None
        token = d.get("token")
        chat_id = d.get("chat_id")
        if not token or not chat_id:
            return None
        proxy = d.get("proxy") or d.get("https_proxy")
        return cls(token=str(token), chat_id=str(chat_id), proxy=str(proxy) if proxy else None)


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig) -> None:
        self.cfg = cfg
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """Build urllib opener with proxy support."""
        if not self.cfg.proxy:
            return urllib.request.build_opener()
        proxy = self.cfg.proxy
        if proxy.startswith("socks5://") or proxy.startswith("socks5h://"):
            # SOCKS5 via PySocks
            try:
                import socks  # type: ignore[import-untyped]
                import socket
                parsed = urllib.parse.urlparse(proxy)
                socks.set_default_proxy(
                    socks.SOCKS5,
                    parsed.hostname or "localhost",
                    parsed.port or 1080,
                    username=parsed.username or None,
                    password=parsed.password or None,
                )
                socket.socket = socks.socksocket  # type: ignore[misc]
                log.info("SOCKS5 proxy configured for Telegram: %s", parsed.hostname)
                return urllib.request.build_opener()
            except ImportError:
                log.error("PySocks not installed but SOCKS5 proxy configured. Install: pip install pysocks")
                raise
        # HTTP/HTTPS proxy
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})]
        if proxy.startswith("http://"):
            # Disable SSL verification for HTTP proxy (optional, depends on environment)
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        opener = urllib.request.build_opener(*handlers)
        log.info("HTTP proxy configured for Telegram")
        return opener

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.cfg.token}/{method}"

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(method),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw)
                if not result.get("ok"):
                    log.warning("Telegram API error: %s", result.get("description"))
                    return None
                return result.get("result")
        except urllib.error.HTTPError as exc:
            log.warning("Telegram HTTP error %s: %s", exc.code, exc.read().decode("utf-8", errors="ignore"))
            return None
        except Exception as exc:
            log.warning("Telegram request failed: %s", exc)
            return None

    def send(self, text: str, *, parse_mode: str = "Markdown") -> dict[str, Any] | None:
        if len(text) > 4096:
            chunks = []
            while text:
                chunk = text[:4096]
                text = text[4096:]
                chunks.append(chunk)
            results = []
            for chunk in chunks:
                r = self._post("sendMessage", {
                    "chat_id": self.cfg.chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                })
                results.append(r)
            return results[-1] if results else None

        return self._post("sendMessage", {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        })

    # ── helpers ──────────────────────────────────────────────────

    def new_response(self, employer: str, vacancy: str, snippet: str) -> None:
        self.send(
            f"📬 *Новый отклик*\n"
            f"*{employer}*\n"
            f"{vacancy}\n\n"
            f"{snippet[:300]}{'…' if len(snippet) > 300 else ''}"
        )

    def test_task(self, employer: str, vacancy: str, details: str = "") -> None:
        self.send(
            f"📝 *Тестовое задание*\n"
            f"*{employer}*\n"
            f"{vacancy}\n\n"
            f"{details[:500]}{'…' if len(details) > 500 else ''}"
        )

    def interview_invite(self, employer: str, vacancy: str, details: str = "") -> None:
        self.send(
            f"🗓 *Приглашение на собеседование*\n"
            f"*{employer}*\n"
            f"{vacancy}\n\n"
            f"{details[:500]}{'…' if len(details) > 500 else ''}"
        )

    def daily_report(self, text: str) -> None:
        self.send(f"📊 *Ежедневный отчёт*\n\n{text}")


def load_notifier(cfg: dict[str, Any]) -> TelegramNotifier | None:
    tg = TelegramConfig.from_dict(cfg.get("notifications", {}).get("telegram"))
    if not tg:
        log.info("Telegram notifications disabled (no config)")
        return None
    return TelegramNotifier(tg)
