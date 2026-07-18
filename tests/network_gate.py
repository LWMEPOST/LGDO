from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from collections.abc import Mapping
from urllib.parse import urlparse


_FIXED_RUNTIME_PORTS = frozenset({8000, 8011, 8787})
_LOCAL_POSTGRES_PORTS = frozenset({5432})
_NETWORK_SECRET_KEYS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "GBRAIN_API_KEY",
        "GBRAIN_QUERY_API_KEY",
        "GBRAIN_PROJECTION_API_KEY",
    }
)


def _network_target_allowed(address: object) -> bool:
    """Allow only local test services, never the user's fixed runtime ports."""
    if isinstance(address, str):
        # AF_UNIX/Windows named-pipe addresses are local transports, not TCP.
        return True
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    host = str(address[0]).strip().lower().strip("[]")
    try:
        port = int(address[1])
    except (TypeError, ValueError):
        return False
    if not 1 <= port <= 65535:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        loopback = True
    else:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        return False
    if port in _FIXED_RUNTIME_PORTS:
        return False
    return port in _LOCAL_POSTGRES_PORTS or loopback


def _sanitized_child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    values = {str(key): str(value) for key, value in source.items()}
    if os.environ.get("LGDO_ALLOW_EXTERNAL_TEST_CREDENTIALS") == "1":
        return values
    for key in _NETWORK_SECRET_KEYS:
        values.pop(key, None)
    endpoint = values.get("GBRAIN_ENDPOINT", "")
    if endpoint and not _local_endpoint(endpoint):
        values.pop("GBRAIN_ENDPOINT", None)
    return values


def _local_endpoint(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return _network_target_allowed((host, port))
    except (TypeError, ValueError):
        return False


def _postgres_target_allowed(host: object, port: object) -> bool:
    return _network_target_allowed((host or "localhost", int(port or 5432)))


try:
    import psycopg
except ImportError:  # pragma: no cover - optional in lightweight environments
    psycopg = None
else:
    _ORIGINAL_PSYCOPG_CONNECT = psycopg.connect

    def _guarded_psycopg_connect(*args, **kwargs):
        host = kwargs.get("host")
        port = kwargs.get("port")
        if args and isinstance(args[0], str) and args[0].strip():
            try:
                from psycopg.conninfo import conninfo_to_dict

                conninfo = conninfo_to_dict(args[0])
                host = host or conninfo.get("host")
                port = port or conninfo.get("port")
            except Exception:
                pass
        if not _postgres_target_allowed(host, port):
            raise PermissionError(
                f"test network gate denied PostgreSQL connection to {host}:{port}"
            )
        return _ORIGINAL_PSYCOPG_CONNECT(*args, **kwargs)


_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex


def _guarded_socket_connect(sock: socket.socket, address: object):
    if not _network_target_allowed(address):
        raise PermissionError(f"test network gate denied connection to {address!r}")
    return _ORIGINAL_SOCKET_CONNECT(sock, address)


def _guarded_socket_connect_ex(sock: socket.socket, address: object):
    if not _network_target_allowed(address):
        raise PermissionError(f"test network gate denied connection to {address!r}")
    return _ORIGINAL_SOCKET_CONNECT_EX(sock, address)


_ORIGINAL_POPEN = subprocess.Popen


def _guarded_popen(*args, **kwargs):
    kwargs["env"] = _sanitized_child_env(kwargs.get("env"))
    return _ORIGINAL_POPEN(*args, **kwargs)


def install_network_gate() -> None:
    if psycopg is not None and not getattr(
        psycopg, "_lgdo_network_gate_installed", False
    ):
        psycopg.connect = _guarded_psycopg_connect
        psycopg._lgdo_network_gate_installed = True

    if not getattr(socket.socket, "_lgdo_network_gate_installed", False):
        socket.socket.connect = _guarded_socket_connect
        socket.socket.connect_ex = _guarded_socket_connect_ex
        socket.socket._lgdo_network_gate_installed = True

    if not getattr(subprocess, "_lgdo_network_gate_installed", False):
        subprocess.Popen = _guarded_popen
        subprocess._lgdo_network_gate_installed = True

    for secret_key in _NETWORK_SECRET_KEYS:
        os.environ.pop(secret_key, None)
    endpoint = os.environ.get("GBRAIN_ENDPOINT")
    if endpoint and not _local_endpoint(endpoint):
        os.environ.pop("GBRAIN_ENDPOINT", None)
