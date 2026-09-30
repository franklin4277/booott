import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)


class CriticalNotifier:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=5)
        self.pagerduty_key = os.environ.get("PAGERDUTY_ROUTING_KEY", "")
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    async def send_critical(
        self,
        summary: str,
        details: str,
        *,
        dedup_key: str | None = None,
    ) -> None:
        tasks = []
        if self.pagerduty_key:
            tasks.append(self._send_pagerduty(summary, details, dedup_key))
        if self.telegram_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(summary, details))
        if not tasks:
            logger.critical(
                "No external critical-alert destination configured: %s | %s",
                summary,
                details,
            )
            return
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.exception(
                    "Critical alert delivery failed",
                    exc_info=(type(outcome), outcome, outcome.__traceback__),
                )

    async def _send_pagerduty(
        self, summary: str, details: str, dedup_key: str | None
    ) -> None:
        payload = {
            "routing_key": self.pagerduty_key,
            "event_action": "trigger",
            "payload": {
                "summary": summary,
                "source": "mt5-state-reconciliation",
                "severity": "critical",
                "custom_details": {"details": details},
            },
        }
        if dedup_key:
            payload["dedup_key"] = dedup_key
        response = await self.client.post(
            "https://events.pagerduty.com/v2/enqueue",
            json=payload,
        )
        response.raise_for_status()

    async def _send_telegram(self, summary: str, details: str) -> None:
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            json={
                "chat_id": self.telegram_chat_id,
                "text": f"CRITICAL: {summary}\n{details}",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError("Telegram did not confirm critical-alert delivery.")

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
