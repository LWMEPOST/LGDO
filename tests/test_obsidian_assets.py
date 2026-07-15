from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime as real_datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import pytest
from ruamel.yaml import YAML

from app import obsidian
from app.obsidian import build_obsidian_uri, ensure_obsidian_vault, main
from app import vault as vault_module
from app.vault import ensure_vault


REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGED_PLUGINS = [
    "file-explorer",
    "global-search",
    "switcher",
    "backlink",
    "outgoing-link",
    "tag-pane",
    "page-preview",
    "templates",
    "outline",
    "word-count",
]


def _powershell_hosts() -> list[str]:
    hosts: list[str] = []
    if os.name == "nt":
        windows_powershell = shutil.which("powershell.exe")
        if windows_powershell:
            hosts.append(windows_powershell)
    pwsh = shutil.which("pwsh")
    if pwsh and pwsh not in hosts:
        hosts.append(pwsh)
    return hosts


POWERSHELL_HOSTS = _powershell_hosts()


def test_versioned_obsidian_assets_are_safe_and_complete():
    resource_root = obsidian.RESOURCE_ROOT
    obsidian_dir = resource_root / ".obsidian"

    assert json.loads((obsidian_dir / "app.json").read_text(encoding="utf-8")) == {
        "alwaysUpdateLinks": True,
        "newFileLocation": "folder",
        "newFileFolderPath": "wiki",
        "showUnsupportedFiles": False,
        "useMarkdownLinks": False,
    }
    assert json.loads((obsidian_dir / "core-plugins.json").read_text(encoding="utf-8")) == MANAGED_PLUGINS
    assert json.loads((obsidian_dir / "templates.json").read_text(encoding="utf-8")) == {
        "folder": "templates"
    }

    template = (resource_root / "templates" / "Wiki Page.md").read_text(encoding="utf-8")
    frontmatter = YAML(typ="safe").load(template.split("---", 2)[1])
    assert frontmatter == {
        "title": "",
        "source_ids": [],
        "domain": "",
        "page_type": "",
        "review_status": "draft",
        "owner": None,
        "tags": [],
        "aliases": [],
    }
    assert "lgdo_page_id" not in template
    assert "lgdo_revision_id" not in template
    assert "lgdo_write_token" not in template

    home = (resource_root / "indexes" / "Home.md").read_text(encoding="utf-8")
    for expected in ("Wiki domains", "Reviews", "Sync status", "Logs"):
        assert expected in home
    readme = (resource_root / "README.md").read_text(encoding="utf-8")
    assert "editable" in readme.lower()
    assert "read-only" in readme.lower()

    assert not list(obsidian_dir.glob("workspace*.json"))
    assert not (obsidian_dir / "plugins").exists()
    assert not (resource_root / ".trash").exists()

    ignore_lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/resources/obsidian-vault/.obsidian/workspace*.json" in ignore_lines
    assert "/resources/obsidian-vault/.obsidian/plugins/" in ignore_lines
    assert "/resources/obsidian-vault/.trash/" in ignore_lines
    assert "vault/" in ignore_lines


def test_install_copies_missing_assets_and_leaves_drift_untouched(tmp_path):
    vault = tmp_path / "vault"
    expected = sorted(
        [
            ".obsidian/app.json",
            ".obsidian/core-plugins.json",
            ".obsidian/templates.json",
            "README.md",
            "indexes/Home.md",
            "templates/Wiki Page.md",
        ]
    )

    installed = ensure_obsidian_vault(vault)
    assert installed.vault_path == vault.resolve()
    assert installed.installed == expected
    assert installed.drifted == []
    assert installed.backup_dir is None

    app_path = vault / ".obsidian" / "app.json"
    drifted_bytes = b'{"alwaysUpdateLinks": false, broken json\n'
    app_path.write_bytes(drifted_bytes)
    result = ensure_obsidian_vault(vault)

    assert result.installed == []
    assert result.drifted == [".obsidian/app.json"]
    assert result.backup_dir is None
    assert app_path.read_bytes() == drifted_bytes


def test_refreshes_in_same_second_use_distinct_backups_and_merge_user_json(tmp_path, monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 7, 15, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(obsidian, "datetime", FixedDateTime)
    vault = tmp_path / "vault"
    ensure_obsidian_vault(vault)
    app_path = vault / ".obsidian" / "app.json"

    prior_one = b'{"alwaysUpdateLinks": false, "customSetting": "first"}\n'
    app_path.write_bytes(prior_one)
    first = ensure_obsidian_vault(vault, refresh=True)

    prior_two = b'{"newFileFolderPath": "elsewhere", "customSetting": "second"}\n'
    app_path.write_bytes(prior_two)
    second = ensure_obsidian_vault(vault, refresh=True)

    assert first.backup_dir is not None
    assert second.backup_dir is not None
    assert first.backup_dir != second.backup_dir
    assert first.backup_dir.parent == vault / ".lgdo" / "obsidian-backups"
    assert second.backup_dir.parent == first.backup_dir.parent
    assert re.fullmatch(r"20260715T123456Z-[0-9a-f]{32}", first.backup_dir.name)
    assert re.fullmatch(r"20260715T123456Z-[0-9a-f]{32}", second.backup_dir.name)
    assert (first.backup_dir / ".obsidian" / "app.json").read_bytes() == prior_one
    assert (second.backup_dir / ".obsidian" / "app.json").read_bytes() == prior_two

    merged = json.loads(app_path.read_text(encoding="utf-8"))
    assert merged["customSetting"] == "second"
    assert merged["newFileFolderPath"] == "wiki"


def test_refresh_merges_all_json_configs_and_replaces_non_json_assets(tmp_path):
    vault = tmp_path / "vault"
    ensure_obsidian_vault(vault)

    app_path = vault / ".obsidian" / "app.json"
    app_path.write_text(
        json.dumps({"newFileFolderPath": "notes", "theme": "moon"}),
        encoding="utf-8",
    )
    plugins_path = vault / ".obsidian" / "core-plugins.json"
    plugins_path.write_text(
        json.dumps(["tag-pane", "third-party-plugin", "backlink", "third-party-plugin"]),
        encoding="utf-8",
    )
    templates_path = vault / ".obsidian" / "templates.json"
    templates_path.write_text(
        json.dumps({"folder": "old", "dateFormat": "YYYY-MM-DD"}),
        encoding="utf-8",
    )
    template_path = vault / "templates" / "Wiki Page.md"
    template_path.write_text("user drift\n", encoding="utf-8")
    original_bytes = {
        ".obsidian/app.json": app_path.read_bytes(),
        ".obsidian/core-plugins.json": plugins_path.read_bytes(),
        ".obsidian/templates.json": templates_path.read_bytes(),
        "templates/Wiki Page.md": template_path.read_bytes(),
    }

    result = ensure_obsidian_vault(vault, refresh=True)

    assert result.drifted == []
    assert result.installed == sorted(
        [
            ".obsidian/app.json",
            ".obsidian/core-plugins.json",
            ".obsidian/templates.json",
            "templates/Wiki Page.md",
        ]
    )
    app_config = json.loads(app_path.read_text(encoding="utf-8"))
    assert app_config["theme"] == "moon"
    assert app_config["newFileFolderPath"] == "wiki"
    assert json.loads(plugins_path.read_text(encoding="utf-8")) == MANAGED_PLUGINS + [
        "third-party-plugin"
    ]
    templates_config = json.loads(templates_path.read_text(encoding="utf-8"))
    assert templates_config == {"folder": "templates", "dateFormat": "YYYY-MM-DD"}
    assert template_path.read_bytes() == (obsidian.RESOURCE_ROOT / "templates" / "Wiki Page.md").read_bytes()
    assert result.backup_dir is not None
    for relative_path, expected_bytes in original_bytes.items():
        assert (result.backup_dir / relative_path).read_bytes() == expected_bytes
    assert list(result.backup_dir.parent.iterdir()) == [result.backup_dir]


def test_json_refresh_replace_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    ensure_obsidian_vault(vault)
    app_path = vault / ".obsidian" / "app.json"
    original = b'{"alwaysUpdateLinks": false, "customSetting": true}\n'
    app_path.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("replace blocked")

    monkeypatch.setattr(obsidian.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace blocked"):
        ensure_obsidian_vault(vault, refresh=True)

    assert app_path.read_bytes() == original
    assert not list(app_path.parent.glob(".app.json.*.tmp"))


@pytest.mark.parametrize("host", POWERSHELL_HOSTS, ids=lambda host: Path(host).stem)
def test_install_script_works_from_unrelated_cwd_with_absolute_vault(host, tmp_path):
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    vault = tmp_path / "absolute vault"
    script = REPO_ROOT / "scripts" / "install-obsidian-vault.ps1"

    completed = subprocess.run(
        [
            host,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-VaultPath",
            str(vault),
        ],
        cwd=unrelated,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["operation"] == "install"
    assert payload["vault_path"] == str(vault.resolve())
    assert not completed.stderr.strip()
    assert json.loads((vault / ".obsidian" / "app.json").read_text(encoding="utf-8"))[
        "newFileFolderPath"
    ] == "wiki"


def test_powershell_scripts_use_safe_path_and_protocol_contracts():
    install_source = (REPO_ROOT / "scripts" / "install-obsidian-vault.ps1").read_text(
        encoding="utf-8"
    )
    assert "[string]$VaultPath = 'vault'" in install_source
    assert "$ErrorActionPreference = 'Stop'" in install_source
    assert "[System.IO.Path]::GetFullPath" in install_source

    open_source = (REPO_ROOT / "scripts" / "open-obsidian.ps1").read_text(encoding="utf-8")
    assert "$ErrorActionPreference = 'Stop'" in open_source
    assert "--page-path $PagePath" in open_source
    assert "ConvertFrom-Json).url" in open_source
    assert "Unable to open Obsidian URI '$uri'" in open_source


@pytest.mark.parametrize("host", POWERSHELL_HOSTS, ids=lambda host: Path(host).stem)
def test_install_script_resolves_relative_vault_from_repo_root(host, tmp_path):
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    vault = REPO_ROOT / ".codex-runtime" / f"relative vault {uuid4().hex}"
    relative_vault = str(vault.relative_to(REPO_ROOT))
    script = REPO_ROOT / "scripts" / "install-obsidian-vault.ps1"

    try:
        completed = subprocess.run(
            [
                host,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-VaultPath",
                relative_vault,
            ],
            cwd=unrelated,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["operation"] == "install"
        assert payload["vault_path"] == str(vault.resolve())
    finally:
        shutil.rmtree(vault, ignore_errors=True)


def test_build_obsidian_uri_encodes_unicode_spaces_nested_paths_and_reserved_characters():
    vault_name = "团队 知识#?&%"
    page_path = r"产品 文档\嵌套/页面 #?&%.md"
    normalized_path = page_path.replace("\\", "/")
    expected = "obsidian://open?" + urlencode(
        {"vault": vault_name, "file": normalized_path},
        quote_via=quote,
    )

    uri = build_obsidian_uri(vault_name, page_path)

    assert uri == expected
    assert "+" not in uri
    assert "%2F" in uri
    for encoded in ("%23", "%3F", "%26", "%25"):
        assert encoded in uri


def test_cli_install_and_link_emit_json_and_return_zero(tmp_path, capsys):
    vault = tmp_path / "vault"

    assert main(["install", "--vault", str(vault)]) == 0
    install_output = json.loads(capsys.readouterr().out)
    assert install_output["operation"] == "install"
    assert install_output["vault_path"] == str(vault.resolve())
    assert ".obsidian/app.json" in install_output["installed"]

    assert main(
        [
            "link",
            "--vault-name",
            "团队 知识",
            "--page-path",
            "产品/页面 #?&%.md",
        ]
    ) == 0
    link_output = json.loads(capsys.readouterr().out)
    assert link_output == {
        "operation": "link",
        "url": build_obsidian_uri("团队 知识", "产品/页面 #?&%.md"),
    }


def test_cli_errors_are_json_on_stderr_and_return_two(tmp_path, capsys, monkeypatch):
    def fail_install(vault_path, refresh=False):
        raise OSError("cannot install")

    monkeypatch.setattr(obsidian, "ensure_obsidian_vault", fail_install)

    assert main(["install", "--vault", str(tmp_path / "vault")]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "cannot install"}


@pytest.mark.parametrize(
    "argv",
    [
        ["install"],
        ["unknown-command"],
    ],
    ids=["missing-required-argument", "unknown-command"],
)
def test_cli_parse_errors_are_single_json_on_stderr(argv, capsys):
    assert main(argv) == 2

    captured = capsys.readouterr()
    assert not captured.out
    error = json.loads(captured.err)
    assert set(error) == {"error"}
    assert error["error"]


def test_module_cli_missing_argument_exits_two_with_json_stderr():
    completed = subprocess.run(
        [sys.executable, "-m", "app.obsidian", "install"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 2
    assert not completed.stdout
    error = json.loads(completed.stderr)
    assert set(error) == {"error"}
    assert error["error"]


def test_benchmark_note_names_the_versioned_indexes_directory():
    benchmark_source = (REPO_ROOT / "scripts" / "rag_benchmark_test_data.py").read_text(
        encoding="utf-8"
    )
    assert "vault/raw, normalized, jsonl, wiki, indexes and logs materialized on disk" in benchmark_source
    assert "vault/raw, normalized, jsonl, wiki, index and logs materialized on disk" not in benchmark_source


def test_legacy_index_migration_moves_deduplicates_and_quarantines(tmp_path):
    vault = tmp_path / "vault"
    legacy = vault / "index"
    indexes = vault / "indexes"
    (legacy / "nested").mkdir(parents=True)
    indexes.mkdir(parents=True)
    (legacy / "nested" / "missing.md").write_text("move me\n", encoding="utf-8")
    (legacy / "duplicate.md").write_text("same\n", encoding="utf-8")
    (indexes / "duplicate.md").write_text("same\n", encoding="utf-8")
    (legacy / "conflict.md").write_text("legacy\n", encoding="utf-8")
    (indexes / "conflict.md").write_text("current\n", encoding="utf-8")

    ensure_vault(vault)

    assert (indexes / "nested" / "missing.md").read_text(encoding="utf-8") == "move me\n"
    assert (indexes / "duplicate.md").read_text(encoding="utf-8") == "same\n"
    assert (indexes / "conflict.md").read_text(encoding="utf-8") == "current\n"
    assert not legacy.exists()
    quarantine_parent = vault / ".lgdo" / "index-migration"
    quarantine_roots = list(quarantine_parent.iterdir())
    assert len(quarantine_roots) == 1
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{32}", quarantine_roots[0].name)
    assert (quarantine_roots[0] / "conflict.md").read_text(encoding="utf-8") == "legacy\n"

    ensure_vault(vault)
    assert list(quarantine_parent.iterdir()) == quarantine_roots


def test_ensure_vault_does_not_install_obsidian_assets_implicitly(tmp_path):
    source = inspect.getsource(vault_module.ensure_vault)
    assert "ensure_obsidian_vault" not in source

    vault = tmp_path / "vault"
    ensure_vault(vault)
    assert not (vault / ".obsidian").exists()


@pytest.mark.parametrize("host", POWERSHELL_HOSTS, ids=lambda host: Path(host).stem)
def test_open_script_print_only_exactly_matches_python_uri(host, tmp_path):
    unrelated = tmp_path / "unrelated cwd"
    unrelated.mkdir()
    script = REPO_ROOT / "scripts" / "open-obsidian.ps1"
    vault_name = "团队 知识#?&%"
    page_path = "产品 文档/嵌套/页面 #?&%.md"

    completed = subprocess.run(
        [
            host,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-VaultName",
            vault_name,
            "-PagePath",
            page_path,
            "-PrintOnly",
        ],
        cwd=unrelated,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == build_obsidian_uri(vault_name, page_path)
    assert not completed.stderr.strip()
