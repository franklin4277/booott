import os
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import httpx

from services.reconciliation.app.models import MT5HostStatus, MT5Snapshot


class MT5AdapterUnavailable(RuntimeError):
    """Raised when the Windows host adapter cannot provide a trusted snapshot."""


class MT5AdapterClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get(
            "MT5_ADAPTER_URL", "http://host.docker.internal:8765"
        )).rstrip("/") + "/"
        parsed_url = urlsplit(self.base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("MT5_ADAPTER_URL must be an absolute HTTP(S) base URL.")
        if (
            parsed_url.scheme != "https"
            and parsed_url.hostname.casefold()
            not in {"host.docker.internal", "localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Use HTTPS for non-loopback MT5 adapter URLs.")
        self.token = token or os.environ.get("MT5_ADAPTER_TOKEN", "")
        if len(self.token.encode("utf-8")) < 32:
            raise ValueError("MT5_ADAPTER_TOKEN must contain at least 32 bytes")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds
            or float(os.environ.get("MT5_ADAPTER_TIMEOUT_SECONDS", "10")),
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def get_snapshot(self, since: datetime | None = None) -> MT5Snapshot:
        params = {"since": since.isoformat()} if since is not None else None
        try:
            response = await self.client.get(
                urljoin(self.base_url, "v1/state"),
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            return MT5Snapshot.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            raise MT5AdapterUnavailable(
                "The MT5 host adapter returned no valid account-state snapshot."
            ) from exc

    async def get_account_status(self) -> MT5HostStatus:
        try:
            response = await self.client.get(
                urljoin(self.base_url, "v1/account"),
                headers={"Authorization": f"******"},
            )
            response.raise_for_status()
            return MT5HostStatus.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            raise MT5AdapterUnavailable(
                "The MT5 host adapter returned no valid account status."
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
