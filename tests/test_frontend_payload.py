from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _function(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_qa_request_payload_does_not_include_client_side_identity_context():
    source = _read("frontend/src/App.tsx")

    ask_function = source.split('async function ask() {', 1)[1].split("\n  async function createAccount", 1)[0]
    ask_form_state = source.split("const [askForm, setAskForm] = useState({", 1)[1].split("\n  });", 1)[0]

    assert "...askForm" not in ask_function
    assert "user_id" not in ask_form_state
    assert "role" not in ask_form_state
    assert "acl_tags" not in ask_form_state


def test_wiki_load_and_save_carry_revision_and_idempotency_contract():
    source = _read("frontend/src/App.tsx")
    load_page = _function(source, "async function loadPage", "\n  async function savePage")
    save_page = _function(source, "async function savePage", "\n  async function markPageStale")

    assert "current_revision_id: page.current_revision_id" in load_page
    assert "expected_revision_id: editor.current_revision_id" in save_page
    assert "request_id: crypto.randomUUID()" in save_page
    assert 'note: "管理端保存"' in save_page

    payload = save_page.split("body: JSON.stringify({", 1)[1].split("}),", 1)[0]
    payload_keys = set(re.findall(r"^\s*([a-z_]+):", payload, flags=re.MULTILINE))
    assert payload_keys == {
        "content",
        "expected_revision_id",
        "request_id",
        "review_status",
        "owner",
        "note",
    }


def test_mark_stale_uses_the_target_page_revision_and_a_new_request_id():
    source = _read("frontend/src/App.tsx")
    mark_stale = _function(source, "async function markPageStale", "\n  async function updateReview")

    assert "pages.find((page) => page.path === path)" in mark_stale
    assert "listedPage?.current_revision_id" in mark_stale
    assert "path === editor.path ? editor.current_revision_id : listedPage?.current_revision_id" in mark_stale
    assert "await api<WikiPageContentResponse>" in mark_stale
    assert "expected_revision_id: targetPage.current_revision_id" in mark_stale
    assert "request_id: crypto.randomUUID()" in mark_stale
    assert mark_stale.count('method: "PATCH"') == 1


def test_api_error_and_frontend_types_expose_revision_projection_contract():
    client = _read("frontend/src/api/client.ts")
    types = _read("frontend/src/types.ts")

    assert "export class ApiError extends Error" in client
    assert "status: number" in client
    assert "detail: unknown" in client
    assert "throw new ApiError" in client
    assert "response.text()" in client

    for field in (
        "page_id",
        "current_revision_id",
        "generated_revision_id",
        "accepted_generated_revision_id",
        "lifecycle_status",
        "projection_epoch",
        "pending_write_intent_id",
        "write_in_progress",
        "write_intent_id",
        "base_revision_id",
        "candidate_revision_id",
        "resolution",
    ):
        assert field in types

    editor = _function(types, "export interface EditorState", "\n}")
    assert "current_revision_id: string" in editor
    assert "lifecycle_status: string" in editor
    assert "projection_epoch: number" in editor


def test_wiki_mutations_update_revision_before_refresh_and_reconcile_conflicts_once():
    source = _read("frontend/src/App.tsx")
    conflict_helper = _function(source, "async function reconcilePageConflict", "\n  async function savePage")
    save_page = _function(source, "async function savePage", "\n  async function markPageStale")
    mark_stale = _function(source, "async function markPageStale", "\n  async function updateReview")

    assert "const wikiMutationInFlight = useRef(false)" in source
    assert conflict_helper.count("await api<WikiPageContentResponse>") == 1
    assert 'method: "PUT"' not in conflict_helper
    assert 'method: "PATCH"' not in conflict_helper
    assert "setEditor((prev)" in conflict_helper
    assert "prev.path !== attemptedPath" in conflict_helper
    assert "content: prev.content" in conflict_helper
    assert "current_revision_id: latestPage.current_revision_id" in conflict_helper
    assert "lifecycle_status: latestPage.lifecycle_status" in conflict_helper
    assert "write_in_progress: latestPage.write_in_progress" in conflict_helper
    assert "write_intent_id: latestPage.write_intent_id" in conflict_helper

    for mutation, method in ((save_page, "PUT"), (mark_stale, "PATCH")):
        assert "if (wikiMutationInFlight.current)" in mutation
        assert "wikiMutationInFlight.current = true" in mutation
        assert "wikiMutationInFlight.current = false" in mutation
        assert mutation.count(f'method: "{method}"') == 1
        assert "error instanceof ApiError && error.status === 409" in mutation
        assert "await reconcilePageConflict(attemptedPath, attemptedContent)" in mutation
        assert "throw error" in mutation
        assert mutation.index("setEditor((prev)") < mutation.index("await refresh()")
        assert "content: prev.content" in mutation
