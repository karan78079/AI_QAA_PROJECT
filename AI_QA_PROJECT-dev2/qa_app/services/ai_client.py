import os
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from openai import APIError, APIStatusError, OpenAI


load_dotenv()


def detect_test_type_from_prompt(test_case: str) -> str:
	text = (test_case or "").lower()
	if re.search(r"\bui\s+testing\s+only\b|\btest\s+type\s*:\s*ui\b", text):
		return "ui"
	if re.search(r"\bapi\s+testing\s+only\b|\btest\s+type\s*:\s*api\b", text):
		return "api"
	api_markers = (
		" api",
		"endpoint",
		"request",
		"response",
		"status code",
		"http method",
		"json body",
		"authorization",
		"bearer",
		"rest api",
		"/api/",
		"post ",
		"get ",
		"put ",
		"patch ",
		"delete ",
	)
	ui_markers = (
		"browser",
		"page",
		"button",
		"input",
		"click",
		"locator",
		"dashboard",
		"login page",
		"visible",
		"playwright",
		"url",
		"ui",
	)
	if any(marker in text for marker in api_markers):
		return "api"
	if any(marker in text for marker in ui_markers):
		return "ui"
	return "ui"


def detect_test_type(test_case: str) -> str:
	return detect_test_type_from_prompt(test_case)


def _normalize_page_url_for_matching(url: str) -> str:
	parts = urlsplit(url.strip())
	path = parts.path.rstrip("/") or "/"
	if parts.fragment.startswith("/") and path != "/":
		path += "/"
	return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _normalize_page_url(url: str) -> str:
	return _normalize_page_url_for_matching(url)


def _client() -> OpenAI:
	api_key = os.getenv("GEMINI_API_KEY")
	if not api_key:
		raise RuntimeError("GEMINI_API_KEY is not configured")

	return OpenAI(
		api_key=api_key,
		base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
	)


def _extract_python_code(content: str) -> str:
	match = re.search(r"```(?:python)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
	return (match.group(1) if match else content).strip() + "\n"


def _code_from_response(content: str) -> str:
	return _extract_python_code(content)


def _extract_json_payload(content: str) -> dict:
	cleaned = content.strip()
	match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
	if match:
		cleaned = match.group(1).strip()
	try:
		return json.loads(cleaned)
	except json.JSONDecodeError as error:
		raise RuntimeError("Gemini returned an invalid test plan") from error


def _json_from_response(content: str) -> dict:
	return _extract_json_payload(content)


def generate_test_plan_for_requirements(requirements: str) -> dict:
	model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
	messages = [
		{
			"role": "system",
			"content": """
You are a senior QA analyst. Read the complete user requirements and return only
valid JSON with this exact shape:
{"test_type":"api" or "ui", "test_cases":[
  {"id":"TC001", "title":"short title", "requirement":"testable scenario", "priority":"high|medium|low"}
]}
Choose API only when the requirements explicitly describe API, REST, HTTP,
endpoints, requests, or responses. Choose UI for browser, page, form, button,
visual, or user-interface requirements. Do not invent scenarios not supported
by the requirements. Create one test case per independently approvable scenario.
If the requirements contain numbered TEST CASE sections, return exactly one
generated test case for every section. Never merge multiple sections into one
test case and never return a summary instead of the complete list.
""",
		},
		{"role": "user", "content": f"REQUIREMENTS:\n{requirements}"},
	]
	response = _request(messages, model)
	content = response.choices[0].message.content or ""
	plan = _extract_json_payload(content)
	if plan.get("test_type") not in {"api", "ui"} or not isinstance(plan.get("test_cases"), list):
		raise RuntimeError("Gemini returned an incomplete test plan")
	if not plan["test_cases"]:
		raise RuntimeError("Gemini returned no test cases")
	expected_cases = len(re.findall(r"^TEST CASE\s+\d+\s*:", requirements, re.MULTILINE | re.IGNORECASE))
	if expected_cases and len(plan["test_cases"]) < expected_cases:
		raise RuntimeError(
			f"Gemini returned {len(plan['test_cases'])} test case(s); "
			f"the requirements contain {expected_cases} TEST CASE sections"
		)
	for index, test_case in enumerate(plan["test_cases"], start=1):
		test_case.setdefault("id", f"TC{index:03d}")
		test_case.setdefault("priority", "medium")
		if not test_case.get("title") or not test_case.get("requirement"):
			raise RuntimeError("Gemini returned an invalid test case")
	return plan


def generate_test_plan(requirements: str) -> dict:
	return generate_test_plan_for_requirements(requirements)


def generate_discovery_test_plan_from_snapshot(url: str, browser_snapshot: str) -> dict:
	model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
	minimum_cases = max(1, int(os.getenv("MIN_GENERATED_TEST_CASES", "7")))
	messages = [
		{
			"role": "system",
			"content": f"""
You are a senior QA analyst exploring a live website. The supplied discovery
context contains snapshots from every reachable same-origin page found by the
browser crawler. Use every page as the source of truth and return only valid
JSON with this shape:
	{{"test_type":"ui", "test_cases":[
		{{"id":"TC001", "page_url":"exact discovered page URL", "title":"short title", "description":"goal",
   "preconditions":["..."], "steps":["..."],
	"expected_result":"...", "priority":"high|medium|low"}}
]}}
Create at least one independently approvable scenario for every discovered
PAGE URL, even when the page only contains navigation or informational content.
Discover realistic browser workflows from all visible pages, navigation, forms,
buttons, and links. Do not focus only on the landing or login page. Create
scenarios without
inventing selectors, products, credentials, payment data, or hidden behavior.
Generate at least {minimum_cases} independently approvable test cases overall.
Distribute them across the discovered pages and cover different workflows such
as navigation, filtering, positive and negative validation, product actions,
cart behavior, order history, and logout when those controls are visible. Do
not pad the list with duplicate cases or invent unsupported behavior. Use
environment variables TEST_EMAIL and TEST_PASSWORD for authentication.
""",
		},
		{
			"role": "user",
			"content": f"WEBSITE URL:\n{url}\n\nBROWSER DISCOVERY SNAPSHOT:\n{browser_snapshot}",
		},
	]
	response = _request(messages, model)
	content = response.choices[0].message.content or ""
	plan = _extract_json_payload(content)
	if plan.get("test_type") != "ui" or not isinstance(plan.get("test_cases"), list):
		raise RuntimeError("Gemini returned an incomplete discovery test plan")
	if not plan["test_cases"]:
		raise RuntimeError("Gemini discovered no test cases")
	if len(plan["test_cases"]) < minimum_cases:
		raise RuntimeError(
			f"Gemini generated {len(plan['test_cases'])} test case(s); "
			f"at least {minimum_cases} are required. Increase discovery context or "
			f"set MIN_GENERATED_TEST_CASES to a lower value."
		)
	for index, test_case in enumerate(plan["test_cases"], start=1):
		test_case.setdefault("id", f"TC{index:03d}")
		test_case.setdefault("page_url", url)
		test_case.setdefault("priority", "medium")
		test_case.setdefault("description", test_case.get("requirement", ""))
		test_case.setdefault("preconditions", [])
		test_case.setdefault("steps", [])
		test_case.setdefault("expected_result", "")
		if not test_case.get("title") or not test_case.get("steps"):
			raise RuntimeError("Gemini returned an invalid discovered test case")
	discovered_pages = {
		_normalize_page_url(line.removeprefix("PAGE URL: ").strip())
		for line in browser_snapshot.splitlines()
		if line.startswith("PAGE URL:")
	}
	covered_pages = {
		_normalize_page_url(test_case["page_url"])
		for test_case in plan["test_cases"]
		if test_case.get("page_url")
	}
	missing_pages = discovered_pages - covered_pages
	if missing_pages:
		raise RuntimeError(
			"Gemini omitted test coverage for discovered page(s): "
			+ ", ".join(sorted(missing_pages))
		)
	return plan


def generate_discovery_test_plan(url: str, browser_snapshot: str) -> dict:
	return generate_discovery_test_plan_from_snapshot(url, browser_snapshot)


def _request_lm_response(messages: list[dict[str, str]], model: str):
	max_attempts = max(1, int(os.getenv("GEMINI_MAX_ATTEMPTS", "4")))
	for attempt in range(max_attempts):
		try:
			return _client().chat.completions.create(
				model=model,
				messages=messages,
			)
		except APIError as error:
			status_code = getattr(error, "status_code", None)
			if status_code == 429:
				raise RuntimeError(
					"Gemini quota is exhausted. Wait for the quota reset or use a Gemini "
					"API key/project with available billing or quota. No retry was attempted."
				) from error
			if status_code in {400, 404}:
				raise RuntimeError(
					f"Gemini rejected model '{model}'. Check GEMINI_MODEL and use a model "
					"supported by the configured Gemini API endpoint."
				) from error
			if status_code not in {500, 502, 503, 504} or attempt == max_attempts - 1:
				raise RuntimeError(
					f"Gemini request failed with HTTP {status_code or 'unknown'} after "
					f"{attempt + 1} attempt(s): {error}"
				) from error
			retry_after = getattr(getattr(error, "response", None), "headers", {}).get("retry-after")
			try:
				delay = max(0, min(60, int(retry_after)))
			except (TypeError, ValueError):
				delay = min(30, 2 ** attempt)
			time.sleep(delay)


def _request(messages: list[dict[str, str]], model: str):
	return _request_lm_response(messages, model)


def generate_api_test_code_for_requirement(test_case: str) -> str:
	model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
	messages = [
		{
			"role": "system",
			"content": """
You generate executable Python pytest tests for REST API validation using the `requests` library.
Return only one complete Python source file, without Markdown fences or explanations.
Use environment variables for secrets and credentials such as API_BASE_URL, API_TOKEN, TEST_EMAIL, TEST_PASSWORD.
Never hardcode real credentials or tokens in the generated source.
Include `from dotenv import load_dotenv` and call `load_dotenv()` so the generated test works when run directly from pytest.
The generated file must contain at least one function named test_... and must be self-contained.
Use `requests.Session()` for reusable clients when helpful.
Write assertions for response status codes, JSON fields, and error handling.
Keep the code minimal, valid Python, and ready to run with pytest.
""",
		},
		{
			"role": "user",
			"content": f"API TEST CASE:\n{test_case}\n",
		},
	]
	response = _request_lm_response(messages, model)
	content = response.choices[0].message.content or ""
	if not content.strip():
		raise RuntimeError("Gemini returned empty API test code")
	return _extract_python_code(content)


def generate_api_test_code(test_case: str) -> str:
	return generate_api_test_code_for_requirement(test_case)


def generate_test_code_for_case(test_case: str, browser_snapshot: str, test_type: str | None = None) -> str:
	test_type = test_type or detect_test_type(test_case)
	if test_type == "api":
		return generate_api_test_code(test_case)
	model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
	messages = [
		{
			"role": "system",
			"content": """
You generate executable Python pytest tests using Playwright's sync API.
Return only one complete Python source file, without Markdown fences or explanations.
Use the supplied browser snapshot as the source of truth for locators.
Use os.getenv('TEST_EMAIL') and os.getenv('TEST_PASSWORD') for credentials;
never copy credential values into the generated source.
Include `from dotenv import load_dotenv` and call `load_dotenv()` so the generated
test works when run directly from pytest. This application uses hash routing;
wait for URLs containing `/#/dashboard/`, `/#/dashboard/cart`,
`/#/dashboard/order`, `/#/dashboard/thanks`, or `/#/auth/login` as appropriate.
Use a specific locator such as `[role='alert']` or `.toast-error`, not a selector
that matches a container and its child at the same time.
Launch every Playwright browser with `headless=True`.
The generated file must contain at least one function named test_... and must be
self-contained, including a sync_playwright context and browser cleanup.
""",
		},
		{
			"role": "user",
			"content": f"USER TEST CASE:\n{test_case}\n\nBROWSER SNAPSHOT:\n{browser_snapshot}",
		},
	]

	response = _request(messages, model)

	content = response.choices[0].message.content or ""
	if not content.strip():
		raise RuntimeError("Gemini returned empty test code")
	return _extract_python_code(content)


def generate_test_code(test_case: str, browser_snapshot: str, test_type: str | None = None) -> str:
	return generate_test_code_for_case(test_case, browser_snapshot, test_type=test_type)


def repair_api_test_code_for_failure(test_case: str, current_code: str, failure_output: str) -> str:
	messages = [
		{
			"role": "system",
			"content": """
You repair an executable Python pytest test that validates REST API endpoints using `requests`.
Return only the complete corrected Python source file, without Markdown fences or explanations.
Preserve the intended API scenario and fix the failing code rather than removing the scenario.
Do not hardcode credentials or tokens. Keep load_dotenv() and use environment variables for secrets.
The result must contain at least one function named test_... and be valid Python.
""",
		},
		{
			"role": "user",
			"content": (
				f"REQUIREMENT:\n{test_case}\n\n"
				f"CURRENT TEST CODE:\n{current_code}\n\n"
				f"PYTEST FAILURE:\n{failure_output}"
			),
		},
	]
	response = _request(messages, os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))
	content = response.choices[0].message.content or ""
	if not content.strip():
		raise RuntimeError("Gemini returned empty repaired API test code")
	return _extract_python_code(content)


def repair_api_test_code(test_case: str, current_code: str, failure_output: str) -> str:
	return repair_api_test_code_for_failure(test_case, current_code, failure_output)


def repair_generated_test_code_for_failure(
	test_case: str,
	browser_snapshot: str,
	current_code: str,
	failure_output: str,
) -> str:
	if detect_test_type(test_case) == "api":
		return repair_api_test_code(test_case, current_code, failure_output)
	messages = [
		{
			"role": "system",
			"content": """
You repair an executable Python pytest test that uses Playwright's sync API.
Return only the complete corrected Python source file, without Markdown fences or
explanations. Preserve the requested coverage, use the browser snapshot as the
source of truth, and do not hardcode credentials. Keep load_dotenv(), use
headless=True, and handle this application's hash-based URLs with /#/ patterns.
Fix the reported failure rather than removing the failing scenario. The result
must contain at least one function named test_... and be valid Python.
""",
		},
		{
			"role": "user",
			"content": (
				f"REQUIREMENT:\n{test_case}\n\n"
				f"BROWSER SNAPSHOT:\n{browser_snapshot}\n\n"
				f"CURRENT TEST CODE:\n{current_code}\n\n"
				f"PYTEST FAILURE:\n{failure_output}"
			),
		},
	]

	response = _request(messages, os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))

	content = response.choices[0].message.content or ""
	if not content.strip():
		raise RuntimeError("Gemini returned empty repaired test code")
	return _extract_python_code(content)


def repair_test_code(
	test_case: str,
	browser_snapshot: str,
	current_code: str,
	failure_output: str,
) -> str:
	return repair_generated_test_code_for_failure(
		test_case,
		browser_snapshot,
		current_code,
		failure_output,
	)
