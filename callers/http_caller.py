#!/usr/bin/env python3
"""
HttpCaller — today's HTTP transport, extracted unchanged behind Caller.
=========================================================================

This is a pure extraction, not new logic: every line of actual HTTP
mechanics here already existed in ``connection_pool.py``'s
``pooled_request`` and ``race_proxy_core.py``'s
``Backend._do_request``. Splitting it into its own file lets
``Backend`` hold a swappable ``self.caller`` instead of hardcoding an
HTTP call, which is the whole point of the Strategy split — see
``callers/base.py``'s module docstring for why that matters
concretely (the devpass CLI-only case).
"""
from __future__ import annotations

from connection_pool import ConnectionPoolManager, GLOBAL_POOL_MANAGER, pooled_request

from callers.base import Caller


class HttpCaller(Caller):
    """Reaches a backend over pooled HTTP(S), exactly as every backend
    in this proxy did before the caller split existed.

    *base_url* / *path* mirror ``pooled_request``'s own parameters
    (see that function's docstring for the base_url-path-joining
    behavior, unrelated to this split and unchanged here).
    """

    def __init__(
        self, base_url: str, path: str = "/chat/completions",
        pool_manager: ConnectionPoolManager = GLOBAL_POOL_MANAGER,
    ):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.pool_manager = pool_manager

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        return pooled_request(
            base_url=self.base_url, path=self.path, method="POST",
            body=payload_bytes, headers=headers, timeout=timeout,
            pool_manager=self.pool_manager,
        )
