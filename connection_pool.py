#!/usr/bin/env python3
"""
hermes-race-proxy: connection pooling
========================================

The same pattern a database client uses to avoid opening a fresh
connection per query: pre-open a small set of persistent connections
per backend host, hand them out on checkout, take them back on
checkin, and validate/replace ones the far end silently closed while
idle. Without this, every single chat-completion call pays a full
DNS+TCP+TLS handshake, often 100-300ms on its own, before the model
ever sees a token, because :mod:`urllib.request` opens and tears down
a new connection every call.

Two independent pools, both DB-pool-shaped:

1. :class:`HTTPConnectionPool`, one per (host, port, scheme), keeps a
   small set of live ``http.client`` connections to a single backend.
   ``acquire()`` / ``release()`` mirror a DB driver's
   checkout/checkin; a connection the server closed while idle (every
   real DB pool's #1 correctness problem, "stale connection" /
   HikariCP's `pool_pre_ping`) is detected on first use and replaced
   transparently, once, rather than surfacing as a client error.

2. :data:`SHARED_EXECUTOR`, one process-wide :class:`ThreadPoolExecutor`
   created ONCE at proxy startup instead of per-request. The old code
   built a fresh ``ThreadPoolExecutor(...)`` inside every call to
   ``race()``, the thread-pool equivalent of opening a new DB
   connection pool object for every query instead of reusing one
   created at application startup.

Both are zero-dependency stdlib (``http.client``, ``concurrent.futures``,
``threading``), no ``requests``/``urllib3``/SQLAlchemy-style pooling
library pulled in, consistent with the rest of this repo.
"""
from __future__ import annotations

import http.client
import logging
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger("race_proxy.connection_pool")


class HTTPConnectionPool:
    """A small, fixed-ceiling pool of persistent connections to ONE
    backend host:port, exactly the shape of a per-database-instance
    connection pool.

    - ``acquire()`` checks out an idle connection if one exists and
      hasn't aged past ``idle_ttl`` (the server-side load balancer /
      keep-alive timeout equivalent of a DB pool's `max_lifetime`);
      otherwise opens a new one, up to ``max_size`` concurrently
      checked-out connections. Beyond that ceiling it BLOCKS the
      caller (like a DB pool under load) until one is released or
      ``timeout`` elapses.
    - ``release(conn, healthy=True)`` returns a connection to the idle
      set for reuse, or closes and discards it if the caller marks it
      unhealthy (a connection error occurred while using it).
    - Idle connections past ``idle_ttl`` are pruned lazily, on the next
      ``acquire()``, no background thread needed for a pool this
      small.
    """

    def __init__(
        self, host: str, port: int, use_https: bool = True,
        max_size: int = 8, idle_ttl: float = 90.0, connect_timeout: float = 15.0,
    ):
        self.host = host
        self.port = port
        self.use_https = use_https
        self.max_size = max_size
        self.idle_ttl = idle_ttl
        self.connect_timeout = connect_timeout
        self._cv = threading.Condition()
        self._idle: list[tuple[http.client.HTTPConnection, float]] = []
        self._in_use_count = 0

    def _new_connection(self) -> http.client.HTTPConnection:
        if self.use_https:
            # Default SSL context, matches urllib.request's default
            # verification behavior (system trust store, hostname check
            # on). No custom context needed for public HTTPS APIs.
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.connect_timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(
            self.host, self.port, timeout=self.connect_timeout,
        )

    def acquire(self, timeout: float = 30.0) -> http.client.HTTPConnection:
        """Check out a connection, blocking if the pool is exhausted.

        Raises TimeoutError if none becomes available within *timeout*,
        the pooled-resource-exhaustion case every DB pool surfaces
        the same way (e.g. HikariCP's `connection is not available,
        request timed out`).
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                # Prune connections idle past their TTL, the far end
                # (or an intermediate load balancer) may have silently
                # closed them; don't hand out something likely dead.
                while self._idle and now - self._idle[-1][1] > self.idle_ttl:
                    stale_conn, _ = self._idle.pop()
                    self._safe_close(stale_conn)
                if self._idle:
                    conn, _ = self._idle.pop()
                    self._in_use_count += 1
                    return conn
                if self._in_use_count < self.max_size:
                    self._in_use_count += 1
                    # Connection creation happens OUTSIDE the lock in
                    # the caller's thread would be nicer, but keeping
                    # it simple here is fine, TCP connect is the slow
                    # part, and only fires on a genuine pool-size
                    # increase, not on every acquire.
                    return self._new_connection()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {timeout}s waiting for a pooled "
                        f"connection to {self.host}:{self.port} "
                        f"(max_size={self.max_size} all in use)"
                    )
                self._cv.wait(timeout=remaining)

    def release(self, conn: http.client.HTTPConnection, healthy: bool = True) -> None:
        """Check a connection back in (reuse) or discard it (on error).

        *healthy* should be False whenever the caller hit a connection
        -level exception using *conn* (broken pipe, reset, remote
        disconnected), never return a connection you're not sure is
        still good, exactly the DB-pool rule of never trusting a
        connection you didn't just validate.
        """
        with self._cv:
            self._in_use_count -= 1
            if healthy:
                self._idle.append((conn, time.monotonic()))
            else:
                self._safe_close(conn)
            self._cv.notify()

    @staticmethod
    def _safe_close(conn: http.client.HTTPConnection) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def stats(self) -> dict:
        """Snapshot for observability, mirrors what a DB pool's metrics
        endpoint typically exposes (active/idle counts)."""
        with self._cv:
            return {
                "host": self.host, "port": self.port,
                "in_use": self._in_use_count, "idle": len(self._idle),
                "max_size": self.max_size,
            }


class ConnectionPoolManager:
    """Registry of one :class:`HTTPConnectionPool` per distinct
    (host, port, scheme), the equivalent of a DB pool manager keeping
    one pool per configured database instance, keyed lazily on first
    use rather than requiring upfront registration.
    """

    def __init__(self, max_size_per_host: int = 8, idle_ttl: float = 90.0):
        self.max_size_per_host = max_size_per_host
        self.idle_ttl = idle_ttl
        self._pools: dict[tuple[str, int, bool], HTTPConnectionPool] = {}
        self._lock = threading.Lock()

    def get_pool(self, host: str, port: int, use_https: bool) -> HTTPConnectionPool:
        key = (host, port, use_https)
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = HTTPConnectionPool(
                    host, port, use_https,
                    max_size=self.max_size_per_host, idle_ttl=self.idle_ttl,
                )
                self._pools[key] = pool
                logger.info(
                    "Created connection pool for %s://%s:%s (max_size=%d)",
                    "https" if use_https else "http", host, port, self.max_size_per_host,
                )
            return pool

    def stats(self) -> list[dict]:
        with self._lock:
            return [p.stats() for p in self._pools.values()]


#: Process-wide connection pool manager. One instance is enough, pools
#: are keyed per-host internally, so every backend in the proxy shares
#: this one manager without contending on each other's connections.
GLOBAL_POOL_MANAGER = ConnectionPoolManager()


def pooled_request(
    base_url: str, path: str, method: str, body: bytes, headers: dict,
    timeout: float, pool_manager: ConnectionPoolManager = GLOBAL_POOL_MANAGER,
) -> tuple[int, bytes]:
    """Issue one HTTP request using a pooled connection, with the
    classic DB-pool "stale connection" retry: if the pooled connection
    turns out to have been closed by the far end while idle (visible
    only when we try to actually use it, TCP has no reliable way to
    detect a half-closed peer without sending data), discard it and
    retry ONCE with a freshly opened connection. A second failure
    propagates, that's a real error, not a stale-pool artifact.

    *base_url*'s own path component (e.g. the ``/zen/v1`` in
    ``https://opencode.ai/zen/v1``) is preserved and joined with *path*:
    callers pass the same *base_url* they'd hand to any HTTP client
    library, not just a bare ``scheme://host:port``.

    Returns (status_code, response_body_bytes). Raises the underlying
    exception (e.g. ``socket.timeout``, ``http.client.HTTPException``)
    on a genuine failure (both attempts failed, or the failure isn't
    the stale-connection shape).
    """
    parsed = urlsplit(base_url)
    use_https = parsed.scheme == "https"
    host = parsed.hostname or ""
    port = parsed.port or (443 if use_https else 80)
    # Join base_url's own path (if any) with the request path, a
    # bare urlsplit(base_url).path discard here was a real bug: a
    # base_url like "https://opencode.ai/zen/v1" has to produce
    # "/zen/v1/chat/completions", not just "/chat/completions".
    full_path = parsed.path.rstrip("/") + "/" + path.lstrip("/")
    pool = pool_manager.get_pool(host, port, use_https)

    last_exc: Optional[Exception] = None
    for attempt in range(2):  # original + one stale-connection retry
        conn = pool.acquire(timeout=timeout)
        try:
            # conn.sock is None on a brand-new pooled connection (the
            # socket isn't opened until .connect(), which request()
            # calls lazily) — the `and` short-circuits and this silently
            # NO-OPS, leaving the socket on its constructor-time
            # connect_timeout (15s) as its permanent read timeout too,
            # regardless of the real per-call `timeout` passed in here.
            # A slow-but-healthy backend (reasoning models with a long
            # silent generation gap) then gets killed at ~15s no matter
            # what timeout the caller configured. Ensure the socket
            # exists first, then set the real timeout on it every time.
            if conn.sock is None:
                conn.connect()
            conn.sock.settimeout(timeout)
            conn.request(method, full_path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            data = resp.read()
            pool.release(conn, healthy=True)
            return status, data
        except (
            http.client.RemoteDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            http.client.BadStatusLine,
        ) as e:
            # Shape of "the server closed this idle pooled connection
            # out from under us", the one class of error a connection
            # pool is expected to paper over transparently, same as a
            # DB driver's pre-ping/validation-query retry.
            pool.release(conn, healthy=False)
            last_exc = e
            if attempt == 0:
                logger.debug(
                    "Stale pooled connection to %s:%s (%s); retrying once with a fresh connection",
                    host, port, e,
                )
                continue
            raise
        except Exception:
            # Any other failure (timeout, DNS, TLS, genuine HTTP error
            # surfaced as an exception) is NOT a stale-connection
            # artifact, discard the connection defensively (we don't
            # know its state) and propagate immediately, no retry.
            pool.release(conn, healthy=False)
            raise
    # Unreachable (the loop always returns or raises), but keeps type
    # checkers happy about a guaranteed return/raise.
    if last_exc:
        raise last_exc
    raise RuntimeError("pooled_request: exhausted retries without a result or exception")
