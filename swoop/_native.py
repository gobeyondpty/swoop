"""Native, pooled family-controlled HTTP. Never rewrites hostnames or SNI."""
from __future__ import annotations

from datetime import timedelta
from typing import Any


class NativeClient:
    """Small primp-shaped facade over wreq's Rust client (no subprocesses)."""

    def __init__(self, family: str, impersonate: str = "chrome", proxy: str | None = None, *, local_address: str | None = None):
        try:
            import wreq
            from wreq.blocking import Client
        except ImportError as exc:
            raise RuntimeError("Family-controlled transport requires Python 3.11+ and swoop-flights[native]") from exc
        profile_name = "Chrome137" if impersonate == "chrome" else "".join(
            part.capitalize() for part in impersonate.split("_"))
        profile = getattr(wreq.Emulation, profile_name, None)
        if profile is None:
            raise ValueError("Unsupported native impersonation profile")
        options: dict[str, Any] = dict(
            emulation=profile, tls_verify=True, tls_verify_hostname=True,
            pool_idle_timeout=timedelta(seconds=90), pool_max_idle_per_host=4,
            pool_max_size=16, tcp_nodelay=True, cookie_store=True,
            redirect=wreq.Policy.none(),
        )
        if proxy:
            options["proxies"] = [wreq.Proxy.all(proxy)]
        else:
            # Explicit direct must ignore both Swoop's default and environment proxies.
            options["no_proxy"] = True
            options["local_address"] = local_address or ("::" if family == "ipv6" else "0.0.0.0")
            strategy = wreq.LookupIpStrategy.IPV6_ONLY if family == "ipv6" else wreq.LookupIpStrategy.IPV4_ONLY
            options["dns_options"] = wreq.DnsOptions(lookup_ip_strategy=strategy)
        self._client = Client(**options)

    def close(self) -> None:
        self._client.close()

    def post(self, url: str, *, content: bytes, headers: dict[str, str], timeout: float) -> Any:
        return self._request("post", url, body=content, headers=headers, timeout=timeout)

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> Any:
        return self._request("get", url, headers=headers, timeout=timeout)

    def _request(self, method: str, url: str, *, timeout: float, **kwargs: Any) -> Any:
        # timedelta preserves fractional budgets; rounding up would extend deadlines.
        response = getattr(self._client, method)(url, timeout=timedelta(microseconds=int(timeout * 1_000_000)), **kwargs)
        return NativeResponse(response)


class NativeResponse:
    def __init__(self, response: Any):
        self._response = response
        self._consumed = False
        self.status_code = response.status.as_int()
        self.headers = response.headers

    def close(self) -> None:
        # wreq.close() tears down the connection even after a fully read body.
        # Keep consumed responses eligible for pooling; abort unread errors.
        if not self._consumed:
            self._response.close()

    @property
    def text(self) -> str:
        try:
            text = self._response.text()
            self._consumed = True
            return text
        except Exception as error:
            # wreq reports a body deadline as BodyError with a TimedOut source.
            # Normalize it without classifying every body failure as a timeout.
            if "source: TimedOut" in str(error):
                raise TimeoutError("Native response body deadline exceeded") from error
            raise
