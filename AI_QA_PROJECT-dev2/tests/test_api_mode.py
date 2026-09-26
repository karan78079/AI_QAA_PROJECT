import json
from types import SimpleNamespace

import ai_client
from ai_client import _json_from_response, detect_test_type
from main import _build_api_url, _load_ui_requirements, _max_repair_attempts, build_requirements_prompt, review_generated_test_cases, validate_api_contract, validate_generated_test_case_coverage


def test_detect_api_case():
    case = """
    Validate POST /api/auth/login endpoint with email and password.
    Check 200 status and JWT token in response.
    """
    assert detect_test_type(case) == "api"


def test_detect_ui_case():
    case = """
    Login through the browser using visible email and password fields.
    Verify dashboard page is shown after clicking login.
    """
    assert detect_test_type(case) == "ui"


def test_parse_json_test_plan():
    plan = _json_from_response('```json\n{"test_type":"api","test_cases":[]}\n```')

    assert plan["test_type"] == "api"


def test_format_approved_requirements():
    requirements = build_requirements_prompt([
        {"id": "TC001", "title": "Login", "requirement": "Accept valid credentials"},
        {"id": "MANUAL001", "title": "Logout", "requirement": "End the session"},
    ])

    assert "TC001: Login" in requirements
    assert "MANUAL001: Logout" in requirements


def test_format_approved_requirements_includes_source_requirement_id():
    requirements = build_requirements_prompt([
        {
            "id": "TC001",
            "requirement_id": "REQ-001",
            "title": "Search Products",
            "requirement": "Search by product name",
        },
    ])

    assert "Source requirement: REQ-001" in requirements


def test_load_ui_requirements_from_configured_file(tmp_path, monkeypatch):
    requirements_file = tmp_path / "ui.md"
    requirements_file.write_text("REQ-001: Search products", encoding="utf-8")
    monkeypatch.setattr("main.ROOT", tmp_path)
    monkeypatch.setenv("UI_REQUIREMENTS_FILE", "ui.md")

    loaded = _load_ui_requirements()

    assert loaded == (str(requirements_file), "REQ-001: Search products")


def test_load_ui_requirements_returns_none_when_default_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("main.ROOT", tmp_path)
    monkeypatch.delenv("UI_REQUIREMENTS_FILE", raising=False)

    assert _load_ui_requirements() is None


def test_max_repair_attempts_uses_configured_value(monkeypatch):
    monkeypatch.setenv("MAX_REPAIR_ATTEMPTS", "5")

    assert _max_repair_attempts() == 5


def test_ui_requirements_plan_preserves_and_covers_requirement_ids(monkeypatch):
    response_content = json.dumps({
        "test_type": "ui",
        "test_cases": [
            {
                "id": "TC001",
                "requirement_id": "REQ-001",
                "title": "Search products",
                "steps": ["Search for a product"],
            },
            {
                "id": "TC002",
                "requirement_id": "REQ-002",
                "title": "Add to cart",
                "steps": ["Add an available product to the cart"],
            },
        ],
    })
    captured = {}

    def fake_request(messages, model):
        captured["prompt"] = messages[1]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))])

    monkeypatch.setattr(ai_client, "_request", fake_request)
    requirements = "REQ-001: Search products\nREQ-002: Add a product to cart"

    plan = ai_client.generate_ui_test_plan_for_requirements(
        requirements,
        "https://example.test/shop",
        "PAGE URL: https://example.test/shop\nbutton: Search",
    )

    assert [case["requirement_id"] for case in plan["test_cases"]] == ["REQ-001", "REQ-002"]
    assert "UI REQUIREMENTS:" in captured["prompt"]
    assert "BROWSER DISCOVERY SNAPSHOT:" in captured["prompt"]


def test_ui_requirements_plan_rejects_missing_requirement_coverage(monkeypatch):
    response_content = json.dumps({
        "test_type": "ui",
        "test_cases": [
            {
                "id": "TC001",
                "requirement_id": "REQ-001",
                "title": "Search products",
                "steps": ["Search for a product"],
            },
        ],
    })
    monkeypatch.setattr(
        ai_client,
        "_request",
        lambda messages, model: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))]
        ),
    )

    try:
        ai_client.generate_ui_test_plan_for_requirements(
            "REQ-001: Search products\nREQ-002: Add to cart",
            "https://example.test/shop",
            "PAGE URL: https://example.test/shop",
        )
    except RuntimeError as error:
        assert "REQ-002" in str(error)
    else:
        raise AssertionError("Expected the planner to reject missing requirement coverage")


def test_ui_code_generation_prompt_requires_python_playwright_syntax(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="def test_sample():\n    assert True"))]
    )

    def fake_request(messages, model):
        captured["system_prompt"] = messages[0]["content"]
        return response

    monkeypatch.setattr(ai_client, "_request", fake_request)

    ai_client.generate_test_code_for_case("TEST TYPE: UI", "button: Submit", test_type="ui")

    assert "snake_case" in captured["system_prompt"]
    assert "toHaveValue" in captured["system_prompt"]


def test_review_can_approve_specific_test(monkeypatch):
    answers = iter(["p", "TC002", "e"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    cases = [
        {"id": "TC001", "title": "Login", "requirement": "Log in", "priority": "high"},
        {"id": "TC002", "title": "Logout", "requirement": "Log out", "priority": "medium"},
    ]

    approved = review_generated_test_cases(cases)

    assert [test_case["id"] for test_case in approved] == ["TC002"]


def test_clearer_names_are_available():
    requirements = build_requirements_prompt([
        {"id": "TC001", "title": "Login", "requirement": "Accept valid credentials"},
    ])

    assert "TC001: Login" in requirements


def test_validate_api_contract_rejects_invalid_html_response():
    class FakeResponse:
        def __init__(self):
            self.status_code = 404
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.text = "<html><body>Cannot GET /product/get-all-products</body></html>"

        def json(self):
            raise ValueError("response is HTML, not JSON")

    def fake_probe(method, path, payload=None):
        return FakeResponse()

    ok, message = validate_api_contract(
        "https://rahulshettyacademy.com",
        "Validate GET /api/products and POST /api/auth/login",
        probe=fake_probe,
    )

    assert ok is False
    assert "404" in message


def test_api_routes_resolve_from_origin_when_base_url_contains_client_path():
    assert _build_api_url(
        "https://rahulshettyacademy.com/client/",
        "/api/ecom/auth/login",
    ) == "https://rahulshettyacademy.com/api/ecom/auth/login"


def test_generated_test_coverage_requires_one_named_function_per_case():
    cases = [{"id": "TC001"}, {"id": "TC002"}]
    source = "def test_tc001_login():\n    pass\ndef test_tc002_logout():\n    pass\n"

    validate_generated_test_case_coverage(source, cases)


def test_generated_test_coverage_normalizes_hyphenated_case_ids():
    cases = [{"id": "TC-017"}]
    source = "def test_tc017_remove_product_from_cart():\n    pass\n"

    validate_generated_test_case_coverage(source, cases)


def test_generated_test_coverage_reports_missing_case_ids():
    cases = [{"id": "TC001"}, {"id": "TC002"}]
    source = "def test_tc001_login():\n    pass\n"

    try:
        validate_generated_test_case_coverage(source, cases)
    except ValueError as error:
        assert "TC002" in str(error)
    else:
        raise AssertionError("Expected missing test-case coverage to be rejected")
