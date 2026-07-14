from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from app.config import Settings
from app.db import init_app_db


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GBRAIN_REPO = PROJECT_ROOT / "gbrain"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_PROVIDER = "openrouter"


class _EmbeddingState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_count = 0
        self._indices: dict[str, int] = {}
        self._used_indices: set[int] = set()

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def record(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self._request_count += 1
            indices = [self._index_for_text(text) for text in texts]
        vectors: list[list[float]] = []
        for index in indices:
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[index] = 1.0
            vectors.append(vector)
        return vectors

    def _index_for_text(self, text: str) -> int:
        existing = self._indices.get(text)
        if existing is not None:
            return existing
        start = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        for offset in range(EMBEDDING_DIMENSIONS):
            candidate = (start + offset) % EMBEDDING_DIMENSIONS
            if candidate not in self._used_indices:
                self._indices[text] = candidate
                self._used_indices.add(candidate)
                return candidate
        raise AssertionError("fake embedding server exhausted its one-hot dimensions")


@dataclass(frozen=True)
class FakeLlamaServer:
    base_url: str
    _state: _EmbeddingState

    @property
    def request_count(self) -> int:
        return self._state.request_count


@dataclass(frozen=True)
class GBrainTestServer:
    endpoint: str
    query_token: str
    projection_token: str
    source_id: str
    root: Path
    process: subprocess.Popen[str]


def _embedding_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="session")
def fake_llama_server() -> Iterator[FakeLlamaServer]:
    state = _EmbeddingState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_json(self, status: int, payload: dict) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path.split("?", 1)[0] == "/v1/models":
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": EMBEDDING_MODEL,
                                "object": "model",
                                "owned_by": "lgdo-e2e",
                            }
                        ],
                    },
                )
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path.split("?", 1)[0] != "/v1/embeddings":
                self._send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length))
                raw_input = payload["input"]
                values = raw_input if isinstance(raw_input, list) else [raw_input]
                texts = [_embedding_text(value) for value in values]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"invalid embedding request: {exc}"})
                return

            vectors = state.record(texts)
            self._send_json(
                200,
                {
                    "object": "list",
                    "model": payload.get("model", EMBEDDING_MODEL),
                    "data": [
                        {"object": "embedding", "index": index, "embedding": vector}
                        for index, vector in enumerate(vectors)
                    ],
                    "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
                },
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    host, port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="lgdo-e2e-llama-server",
    )
    thread.start()
    try:
        yield FakeLlamaServer(base_url=f"http://{host}:{port}", _state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("fake llama-server thread did not terminate")


def _find_bun_executable() -> Path | None:
    candidates: list[Path] = []
    direct = shutil.which("bun.exe")
    if direct:
        candidates.append(Path(direct))

    wrapper = shutil.which("bun")
    if wrapper:
        wrapper_path = Path(wrapper)
        if os.name != "nt" or wrapper_path.suffix.lower() == ".exe":
            candidates.append(wrapper_path)
        candidates.append(wrapper_path.parent / "node_modules" / "bun" / "bin" / "bun.exe")

    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "bun" / "bin" / "bun.exe")
    candidates.append(Path.home() / ".bun" / "bin" / ("bun.exe" if os.name == "nt" else "bun"))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _creation_flags(*, server: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if server:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def _run_bun(
    bun: Path,
    env: dict[str, str],
    *args: str,
    timeout: float = 120,
    cwd: Path = GBRAIN_REPO,
) -> str:
    completed = subprocess.run(
        [os.fspath(bun), *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_creation_flags(),
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Bun command failed "
            f"({completed.returncode}): {args!r}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
    return completed.stdout


def _run_cli(
    bun: Path,
    env: dict[str, str],
    *args: str,
    timeout: float = 120,
) -> str:
    return _run_bun(bun, env, "run", "src/cli.ts", *args, timeout=timeout)


def _parse_client_credentials(output: str) -> tuple[str, str]:
    client_id = re.search(r"Client ID:\s+(\S+)", output)
    client_secret = re.search(r"Client Secret:\s+(\S+)", output)
    if client_id is None or client_secret is None:
        raise AssertionError(f"OAuth registration omitted credentials:\n{output}")
    return client_id.group(1), client_secret.group(1)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(
    process: subprocess.Popen[str],
    health_url: str,
    log_path: Path,
    *,
    timeout_seconds: float = 60,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "health endpoint was not contacted"
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise AssertionError(
                f"GBrain HTTP server exited during startup ({return_code}):\n{log[-6000:]}"
            )
        try:
            response = httpx.get(health_url, timeout=1)
            if response.is_success:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise AssertionError(
        f"GBrain HTTP server did not become healthy: {last_error}\n{log[-6000:]}"
    )


def _mint_token(
    base_url: str,
    *,
    client_id: str,
    client_secret: str,
    scope: str,
) -> str:
    response = httpx.post(
        f"{base_url}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise AssertionError(f"OAuth token response omitted access_token: {payload!r}")
    return token


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        process.wait(timeout=5)
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
            timeout=15,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _is_directory_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _remove_tree_entry(path: Path) -> None:
    if _is_directory_link(path):
        if os.name == "nt" and path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        return
    if path.is_file():
        path.unlink()
        return
    for child in path.iterdir():
        _remove_tree_entry(child)
    path.rmdir()


def _clean_directory(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        _remove_tree_entry(child)


@pytest.fixture(scope="session")
def gbrain_pglite_server(
    tmp_path_factory: pytest.TempPathFactory,
    fake_llama_server: FakeLlamaServer,
) -> Iterator[GBrainTestServer]:
    if os.environ.get("RUN_GBRAIN_E2E") != "1":
        pytest.skip("set RUN_GBRAIN_E2E=1 to run live GBrain acceptance tests")
    bun = _find_bun_executable()
    if bun is None:
        pytest.skip("a directly executable Bun runtime was not found")

    workspace = tmp_path_factory.mktemp("gbrain-pglite-http")
    gbrain_home = (workspace / "gbrain-home").resolve()
    root = (workspace / "vault" / "wiki").resolve()
    database_path = (workspace / "brain.pglite").resolve()
    root.mkdir(parents=True)
    gbrain_home.mkdir(parents=True)
    source_id = "lgdo-e2e"

    env = os.environ.copy()
    no_proxy = ",".join(
        value
        for value in (env.get("NO_PROXY") or env.get("no_proxy"), "127.0.0.1", "localhost")
        if value
    )
    env.update(
        {
            "GBRAIN_HOME": os.fspath(gbrain_home),
            "GBRAIN_IMPORT_ALLOWED_ROOTS": os.fspath(root),
            "GBRAIN_INIT_SKIP_EMBED_CHECK": "1",
            "GBRAIN_NO_BANNER": "1",
            "GBRAIN_NO_SOLE_NON_DEFAULT_NUDGE": "1",
            "GBRAIN_SELF_UPGRADE_MODE": "off",
            "GBRAIN_SKIP_STARTUP_HOOKS": "1",
            "DATABASE_URL": "",
            "GBRAIN_DATABASE_URL": "",
            "NODE_ENV": "test",
            "NO_COLOR": "1",
            "NO_PROXY": no_proxy,
            "OPENROUTER_API_KEY": "lgdo-e2e-offline",
            "OPENROUTER_BASE_URL": f"{fake_llama_server.base_url}/v1",
            "no_proxy": no_proxy,
            "PATH": os.fspath(bun.parent) + os.pathsep + env.get("PATH", ""),
        }
    )

    if not (GBRAIN_REPO / "admin" / "dist" / "index.html").is_file():
        _run_bun(
            bun,
            env,
            "install",
            "--frozen-lockfile",
            "--offline",
            timeout=180,
            cwd=GBRAIN_REPO / "admin",
        )
        _run_bun(
            bun,
            env,
            "run",
            "build",
            timeout=180,
            cwd=GBRAIN_REPO / "admin",
        )

    _run_cli(
        bun,
        env,
        "init",
        "--pglite",
        "--non-interactive",
        "--force",
        "--path",
        os.fspath(database_path),
        "--embedding-model",
        f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}",
        "--embedding-dimensions",
        str(EMBEDDING_DIMENSIONS),
        "--skip-embed-check",
        "--json",
        timeout=180,
    )

    # Keep every embedding request local while exercising GBrain's real
    # OpenAI-compatible transport and semantic cache.
    config_path = gbrain_home / ".gbrain" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider_base_urls = config.get("provider_base_urls")
    if not isinstance(provider_base_urls, dict):
        provider_base_urls = {}
    provider_base_urls[EMBEDDING_PROVIDER] = f"{fake_llama_server.base_url}/v1"
    config["provider_base_urls"] = provider_base_urls
    config_path.write_text(
        json.dumps(config, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _run_cli(
        bun,
        env,
        "sources",
        "add",
        source_id,
        "--path",
        os.fspath(root),
        "--no-federated",
        timeout=60,
    )

    query_output = _run_cli(
        bun,
        env,
        "auth",
        "register-client",
        "lgdo-e2e-query",
        "--grant-types",
        "client_credentials",
        "--scopes",
        "read",
        "--source",
        source_id,
        timeout=60,
    )
    query_client_id, query_client_secret = _parse_client_credentials(query_output)

    projection_output = _run_cli(
        bun,
        env,
        "auth",
        "register-client",
        "lgdo-e2e-projection",
        "--grant-types",
        "client_credentials",
        "--scopes",
        "read write",
        "--source",
        source_id,
        timeout=60,
    )
    projection_client_id, projection_client_secret = _parse_client_credentials(
        projection_output
    )

    configure_source = """
import { loadConfig, toEngineConfig } from './src/core/config.ts';
import { createEngine } from './src/core/engine-factory.ts';
import { readContentChunksEmbeddingDim } from './src/core/embedding-dim-check.ts';
import {
  applyOpenAICompatConfig,
  configureGateway,
  diagnoseEmbedding,
  isAvailable,
} from './src/core/ai/gateway.ts';
import { resolveRecipe } from './src/core/ai/model-resolver.ts';
import { isCacheSafe, resolveEmbeddingColumn } from './src/core/search/embedding-column.ts';
const config = loadConfig();
if (!config) throw new Error('missing isolated GBrain config');
if (config.embedding_disabled === true) throw new Error('embedding remained disabled');
if (config.embedding_model !== process.env.LGDO_EMBEDDING_MODEL) {
  throw new Error(`embedding model mismatch: ${config.embedding_model}`);
}
if (config.embedding_dimensions !== Number(process.env.LGDO_EMBEDDING_DIMENSIONS)) {
  throw new Error(`embedding dimension mismatch: ${config.embedding_dimensions}`);
}
if (config.provider_base_urls?.[process.env.LGDO_EMBEDDING_PROVIDER!] !== process.env.LGDO_EMBEDDING_BASE_URL) {
  throw new Error(`embedding base URL mismatch: ${config.provider_base_urls?.[process.env.LGDO_EMBEDDING_PROVIDER!]}`);
}
const gatewayConfig = {
  embedding_model: config.embedding_model,
  embedding_dimensions: config.embedding_dimensions,
  base_urls: config.provider_base_urls,
  env: process.env,
};
configureGateway(gatewayConfig);
const embeddingDiagnosis = diagnoseEmbedding(config.embedding_model);
const embeddingAvailable = isAvailable('embedding', config.embedding_model);
const embeddingColumn = resolveEmbeddingColumn(undefined, config);
const embeddingCacheSafe = isCacheSafe(embeddingColumn, config);
const { recipe: embeddingRecipe } = resolveRecipe(config.embedding_model!);
const effectiveEmbeddingBaseUrl = applyOpenAICompatConfig(
  embeddingRecipe,
  gatewayConfig,
).baseURL;
const engineConfig = toEngineConfig(config);
const engine = await createEngine(engineConfig);
try {
  await engine.connect(engineConfig);
  const schemaDim = await readContentChunksEmbeddingDim(engine);
  if (!schemaDim.exists || schemaDim.dims !== Number(process.env.LGDO_EMBEDDING_DIMENSIONS)) {
    throw new Error(`schema embedding dimension mismatch: ${JSON.stringify(schemaDim)}`);
  }
  const dbEmbeddingModel = await engine.getConfig('embedding_model');
  const dbEmbeddingDimensions = await engine.getConfig('embedding_dimensions');
  if (dbEmbeddingModel !== process.env.LGDO_EMBEDDING_MODEL) {
    throw new Error(`DB embedding model mismatch: ${dbEmbeddingModel}`);
  }
  if (dbEmbeddingDimensions !== process.env.LGDO_EMBEDDING_DIMENSIONS) {
    throw new Error(`DB embedding dimension mismatch: ${dbEmbeddingDimensions}`);
  }
  const updated = await engine.updateSourceConfig(process.env.LGDO_SOURCE_ID!, {
    lgdo_managed: true,
    lgdo_projection_client_id: process.env.LGDO_PROJECTION_CLIENT_ID!,
  });
  if (!updated) throw new Error('managed source update matched no row');
  const rows = await engine.executeRaw(
    'SELECT local_path, config FROM sources WHERE id = $1',
    [process.env.LGDO_SOURCE_ID!],
  );
  console.log(JSON.stringify({
    ...rows[0],
    embedding_model: config.embedding_model,
    embedding_dimensions: config.embedding_dimensions,
    embedding_base_url: config.provider_base_urls?.[process.env.LGDO_EMBEDDING_PROVIDER!],
    db_embedding_model: dbEmbeddingModel,
    db_embedding_dimensions: Number(dbEmbeddingDimensions),
    schema_embedding_dimensions: schemaDim.dims,
    embedding_diagnosis: embeddingDiagnosis,
    embedding_available: embeddingAvailable,
    embedding_column: embeddingColumn,
    embedding_cache_safe: embeddingCacheSafe,
    effective_embedding_base_url: effectiveEmbeddingBaseUrl,
  }));
} finally {
  await engine.disconnect();
}
"""
    source_env = {
        **env,
        "LGDO_SOURCE_ID": source_id,
        "LGDO_PROJECTION_CLIENT_ID": projection_client_id,
        "LGDO_EMBEDDING_MODEL": f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}",
        "LGDO_EMBEDDING_DIMENSIONS": str(EMBEDDING_DIMENSIONS),
        "LGDO_EMBEDDING_PROVIDER": EMBEDDING_PROVIDER,
        "LGDO_EMBEDDING_BASE_URL": f"{fake_llama_server.base_url}/v1",
    }
    source_output = _run_bun(
        bun,
        source_env,
        "-e",
        configure_source,
        timeout=60,
    )
    source_lines = [line for line in source_output.splitlines() if line.strip()]
    if not source_lines:
        raise AssertionError("managed source verification returned no output")
    source_row = json.loads(source_lines[-1])
    if os.path.normcase(os.path.realpath(source_row["local_path"])) != os.path.normcase(
        os.path.realpath(root)
    ):
        raise AssertionError(f"managed source local_path mismatch: {source_row!r}")
    source_config = source_row.get("config") or {}
    if source_config.get("lgdo_managed") is not True:
        raise AssertionError(f"managed source flag missing: {source_row!r}")
    if source_config.get("lgdo_projection_client_id") != projection_client_id:
        raise AssertionError(f"projection client binding mismatch: {source_row!r}")
    if source_row.get("embedding_model") != f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}":
        raise AssertionError(f"runtime embedding model mismatch: {source_row!r}")
    if source_row.get("embedding_dimensions") != EMBEDDING_DIMENSIONS:
        raise AssertionError(f"runtime embedding dimensions mismatch: {source_row!r}")
    if source_row.get("embedding_base_url") != f"{fake_llama_server.base_url}/v1":
        raise AssertionError(f"runtime embedding URL mismatch: {source_row!r}")
    if source_row.get("db_embedding_model") != f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}":
        raise AssertionError(f"DB embedding model mismatch: {source_row!r}")
    if source_row.get("db_embedding_dimensions") != EMBEDDING_DIMENSIONS:
        raise AssertionError(f"DB embedding dimensions mismatch: {source_row!r}")
    if source_row.get("schema_embedding_dimensions") != EMBEDDING_DIMENSIONS:
        raise AssertionError(f"schema embedding dimensions mismatch: {source_row!r}")
    expected_embedding_model = f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}"
    if source_row.get("embedding_diagnosis") != {
        "ok": True,
        "model": expected_embedding_model,
        "provider": EMBEDDING_PROVIDER,
        "recipeId": EMBEDDING_PROVIDER,
    }:
        raise AssertionError(f"embedding runtime diagnosis mismatch: {source_row!r}")
    if source_row.get("embedding_available") is not True:
        raise AssertionError(f"embedding runtime unavailable: {source_row!r}")
    if source_row.get("embedding_column") != {
        "name": "embedding",
        "type": "vector",
        "dimensions": EMBEDDING_DIMENSIONS,
        "embeddingModel": expected_embedding_model,
    }:
        raise AssertionError(f"embedding column resolution mismatch: {source_row!r}")
    if source_row.get("embedding_cache_safe") is not True:
        raise AssertionError(f"embedding cache is not runtime-safe: {source_row!r}")
    if source_row.get("effective_embedding_base_url") != f"{fake_llama_server.base_url}/v1":
        raise AssertionError(f"effective embedding URL mismatch: {source_row!r}")

    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = workspace / "serve.log"
    log_handle = log_path.open("w", encoding="utf-8", errors="replace")
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [
                os.fspath(bun),
                "run",
                "src/cli.ts",
                "serve",
                "--http",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--public-url",
                base_url,
                "--suppress-bootstrap-token",
            ],
            cwd=GBRAIN_REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(server=True),
            start_new_session=os.name != "nt",
        )
        _wait_for_health(process, f"{base_url}/health", log_path)
        query_token = _mint_token(
            base_url,
            client_id=query_client_id,
            client_secret=query_client_secret,
            scope="read",
        )
        projection_token = _mint_token(
            base_url,
            client_id=projection_client_id,
            client_secret=projection_client_secret,
            scope="read write",
        )
        yield GBrainTestServer(
            endpoint=f"{base_url}/mcp",
            query_token=query_token,
            projection_token=projection_token,
            source_id=source_id,
            root=root,
            process=process,
        )
    finally:
        if process is not None:
            _terminate_process_tree(process)
        log_handle.close()


@pytest.fixture
def gbrain_e2e_settings(
    gbrain_pglite_server: GBrainTestServer,
    tmp_path: Path,
) -> Settings:
    _clean_directory(gbrain_pglite_server.root)
    parent_gitignore = gbrain_pglite_server.root.parent / ".gitignore"
    if parent_gitignore.exists():
        parent_gitignore.unlink()
    outside_root = gbrain_pglite_server.root.parent / "outside-root.md"
    if outside_root.exists() or outside_root.is_symlink():
        outside_root.unlink()
    outside_root_dir = gbrain_pglite_server.root.parent / "outside-root-dir"
    if outside_root_dir.exists() or _is_directory_link(outside_root_dir):
        _remove_tree_entry(outside_root_dir)

    configured = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "lgdo-gbrain-e2e.db",
        vault_path=gbrain_pglite_server.root.parent,
        upload_path=tmp_path / "uploads",
        gbrain_enabled=True,
        gbrain_endpoint=gbrain_pglite_server.endpoint,
        gbrain_api_key=None,
        gbrain_query_api_key=gbrain_pglite_server.query_token,
        gbrain_projection_api_key=gbrain_pglite_server.projection_token,
        gbrain_managed_source_id=gbrain_pglite_server.source_id,
        gbrain_source_id=gbrain_pglite_server.source_id,
        gbrain_import_allowed_root=gbrain_pglite_server.root,
        gbrain_home=gbrain_pglite_server.root.parents[1] / "gbrain-home",
        gbrain_repo_path=GBRAIN_REPO,
        gbrain_import_no_embed=True,
        gbrain_query_expand=False,
        gbrain_query_detail="high",
        gbrain_query_limit=20,
        gbrain_candidate_limit=1,
        gbrain_query_timeout_seconds=30,
        gbrain_incremental_timeout_seconds=120,
        gbrain_reconcile_timeout_seconds=600,
        projection_worker_enabled=False,
        _env_file=None,
    )
    init_app_db(configured)
    assert configured.vault_path / "wiki" == gbrain_pglite_server.root
    assert configured.gbrain_import_allowed_root == gbrain_pglite_server.root
    return configured


def decode_mcp_envelope(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return response.json()
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.removeprefix("data:").strip()
    ]
    if not events:
        raise AssertionError("MCP response contained no SSE data event")
    return events[-1]


def _mcp_text_payload(result: dict) -> tuple[dict | None, str | None]:
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, text
        return (payload if isinstance(payload, dict) else None), text
    return None, None


def _structured_mcp_error(envelope: dict) -> dict | None:
    jsonrpc_error = envelope.get("error")
    if isinstance(jsonrpc_error, dict):
        data = jsonrpc_error.get("data")
        payload = data if isinstance(data, dict) else {}
        return {
            "is_error": True,
            "error": payload.get("error", jsonrpc_error.get("code")),
            "message": payload.get("message", jsonrpc_error.get("message")),
            "jsonrpc_error": jsonrpc_error,
        }

    result = envelope.get("result")
    if not isinstance(result, dict) or not result.get("isError"):
        return None
    structured = result.get("structuredContent")
    payload = structured if isinstance(structured, dict) else None
    text_payload, text = _mcp_text_payload(result)
    if payload is None:
        payload = text_payload or {}
    return {
        "is_error": True,
        "error": payload.get("error"),
        "message": payload.get("message", text),
        "tool_result": result,
    }


@pytest.fixture
def gbrain_mcp_call():
    async def call(
        server: GBrainTestServer,
        *,
        token: str,
        tool: str,
        arguments: dict,
    ) -> dict:
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            initialized = await client.post(
                server.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "initialize",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lgdo-e2e", "version": "1"},
                    },
                },
            )
            decode_mcp_envelope(initialized)
            session_id = initialized.headers.get("mcp-session-id")
            # GBrain's HTTP transport is deliberately stateless today; retain
            # session headers when a future/stateful transport supplies one.
            if session_id:
                headers["mcp-session-id"] = session_id
            ready = await client.post(
                server.endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            ready.raise_for_status()
            response = await client.post(
                server.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "tool-call",
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
        envelope = decode_mcp_envelope(response)
        structured_error = _structured_mcp_error(envelope)
        if structured_error is not None:
            return structured_error
        result = envelope["result"]
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        payload, _ = _mcp_text_payload(result)
        if payload is not None:
            return payload
        raise AssertionError("MCP tool result contained no object payload")

    return call
