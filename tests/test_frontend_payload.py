from pathlib import Path


def test_qa_request_payload_does_not_include_client_side_identity_context():
    app_source = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
    source = app_source.read_text(encoding="utf-8")

    ask_function = source.split('async function ask() {', 1)[1].split("\n  async function createAccount", 1)[0]
    ask_form_state = source.split("const [askForm, setAskForm] = useState({", 1)[1].split("\n  });", 1)[0]

    assert "...askForm" not in ask_function
    assert "user_id" not in ask_form_state
    assert "role" not in ask_form_state
    assert "acl_tags" not in ask_form_state
