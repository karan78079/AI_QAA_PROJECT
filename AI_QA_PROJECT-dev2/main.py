import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from ai_client import generate_discovery_test_plan, generate_test_code, generate_test_plan, repair_test_code
from mcp_client import discover_site
from playwright_runner import classify_failure, run_pytest, validate_python, write_report


ROOT = Path(__file__).resolve().parent
GENERATED_TEST = ROOT / "generated" / "tests" / "test_case.py"
GENERATED_TEST_CASES = ROOT / "generated" / "generated_test_cases.json"
HISTORY_DIR = GENERATED_TEST.parent / "history"
REPAIR_REPORT = ROOT / "reports" / "repair_report.json"


def _extract_api_requirements(requirements_text: str) -> list[tuple[str, str]]:
    explicit_matches = re.findall(r"(GET|POST|PUT|PATCH|DELETE)\s+(/api[^\s,;)]*)", requirements_text, flags=re.IGNORECASE)
    if explicit_matches:
        ordered_requirements: list[tuple[str, str]] = []
        seen: set[str] = set()
        for method, path in explicit_matches:
            cleaned_path = path.strip().rstrip(".,")
            key = (method.upper(), cleaned_path)
            if cleaned_path and key not in seen:
                ordered_requirements.append((method.upper(), cleaned_path))
                seen.add(key)
        return ordered_requirements

    api_paths = re.findall(r"/api[^\s,;)]*", requirements_text)
    ordered_paths: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in api_paths:
        cleaned = path.strip().rstrip(".,")
        if cleaned and cleaned not in seen:
            ordered_paths.append(("GET", cleaned))
            seen.add(cleaned)
    return ordered_paths


def validate_api_contract(base_url: str, requirements_text: str, probe=None) -> tuple[bool, str]:
    if not base_url or not requirements_text:
        return True, "No API contract to validate."

    candidate_routes = _extract_api_requirements(requirements_text)
    if not candidate_routes:
        return True, "No concrete API routes were provided for validation."

    if probe is None:
        import requests

        def probe(method: str, path: str, payload=None):
            request_method = method.upper()
            url = base_url.rstrip("/") + path
            if payload is None:
                return requests.request(request_method, url, timeout=20)
            return requests.request(request_method, url, json=payload, timeout=20)

    issues: list[str] = []
    for method, path in candidate_routes:
        payload = None
        if method == "POST" and "login" in path.lower():
            payload = {"userEmail": "invalid@example.com", "userPassword": "wrongpassword"}
        try:
            response = probe(method, path, payload)
        except Exception as error:  # pragma: no cover - network validation path
            issues.append(f"{method} {path} raised {type(error).__name__}: {error}")
            continue

        content_type = str(response.headers.get("Content-Type", "")).lower()
        if response.status_code in {200, 201, 400, 401, 403, 405}:
            try:
                response.json()
            except Exception:
                if "application/json" not in content_type:
                    issues.append(
                        f"{method} {path} returned {response.status_code} with {content_type or 'unknown'}; "
                        "it did not return JSON as required."
                    )
            continue

        issues.append(f"{method} {path} returned {response.status_code} with {content_type or 'unknown'}.")

    if issues:
        summary = "; ".join(issues[:3])
        if len(issues) > 3:
            summary += " ..."
        return False, f"API contract validation failed: {summary}"
    return True, "API contract validated successfully."


def review_generated_test_cases(test_cases: list[dict]) -> list[dict]:
    approved_test_ids: set[str] = set()
    manual_test_counter = 1
    while True:
        print("\nGenerated test cases:")
        for test_case in test_cases:
            state = "APPROVED" if test_case["id"] in approved_test_ids else "PENDING"
            print(f"[{state}] {test_case['id']} | {test_case['priority']} | {test_case['title']}")
        print("\n[a] approve all  [p] approve specific  [s] reject specific  [m] add manual test  [r] reject all  [e] execute approved")
        choice = input("Review action: ").strip().lower()
        if choice == "a":
            approved_test_ids = {test_case["id"] for test_case in test_cases}
        elif choice == "p":
            selected_ids = {item.strip().upper() for item in input("Test IDs to approve (comma-separated): ").split(",")}
            approved_test_ids.update(test_case["id"] for test_case in test_cases if test_case["id"] in selected_ids)
        elif choice == "r":
            approved_test_ids.clear()
        elif choice == "s":
            rejected_ids = {item.strip().upper() for item in input("Test IDs to reject (comma-separated): ").split(",")}
            approved_test_ids = {test_case["id"] for test_case in test_cases if test_case["id"] not in rejected_ids}
        elif choice == "m":
            manual_test_id = f"MANUAL{manual_test_counter:03d}"
            test_cases.append({
                "id": manual_test_id,
                "title": input("Manual test title: ").strip(),
                "requirement": input("Manual test requirement: ").strip(),
                "priority": input("Priority [high/medium/low]: ").strip().lower() or "medium",
            })
            manual_test_counter += 1
            print(f"Added {manual_test_id}. Approve it with 'a' or review it before execution.")
        elif choice == "e":
            approved_cases = [test_case for test_case in test_cases if test_case["id"] in approved_test_ids]
            if approved_cases:
                return approved_cases
            print("No approved tests. Approve at least one test or reject the plan with 'r'.")
        else:
            print("Unknown action.")


def review_test_cases(test_cases: list[dict]) -> list[dict]:
    return review_generated_test_cases(test_cases)


def build_requirements_prompt(test_cases: list[dict]) -> str:
    return "\n\n".join(
        f"{test_case['id']}: {test_case['title']}\n"
        f"Page URL: {test_case.get('page_url', '')}\n"
        f"Description: {test_case.get('description', test_case.get('requirement', ''))}\n"
        f"Preconditions: {test_case.get('preconditions', [])}\n"
        f"Steps: {test_case.get('steps', [])}\n"
        f"Expected result: {test_case.get('expected_result', '')}"
        for test_case in test_cases
    )


def requirements_for_generation(test_cases: list[dict]) -> str:
    return build_requirements_prompt(test_cases)


def create_repair_report_entry(attempt: int, code: str, failure: str, result: str) -> dict:
    return {
        "repair": attempt,
        "test_code": code,
        "failure": failure,
        "result": result,
    }


def repair_report_entry(attempt: int, code: str, failure: str, result: str) -> dict:
    return create_repair_report_entry(attempt, code, failure, result)


async def main():
    selected_mode = os.getenv("TEST_MODE", "").strip().lower()
    if selected_mode not in {"ui", "api"}:
        selected_mode = input("Testing mode [ui/api]: ").strip().lower()
    if selected_mode not in {"ui", "api"}:
        print("Choose either 'ui' or 'api'.")
        return

    if selected_mode == "ui":
        url = os.getenv("WEBSITE_URL") or input("Website URL: ").strip()
        if not url:
            print("A website URL is required.")
            return
        print("Connecting to MCP and exploring the website...")
        try:
            snapshot = await discover_site(url)
        except RuntimeError as error:
            print(f"Website discovery failed: {error}")
            return
        print("Generating test cases from website discovery with Gemini...")
        try:
            plan = generate_discovery_test_plan(url, snapshot)
        except RuntimeError as error:
            print(f"Test plan generation or validation failed: {error}")
            return
    else:
        url = os.getenv("API_BASE_URL", "").strip()
        requirements_file = os.getenv("API_REQUIREMENTS_FILE", "").strip()

        if not requirements_file:
            default_requirements_path = ROOT / "api_requirements.txt"
            if default_requirements_path.exists():
                requirements_file = str(default_requirements_path)
                print(f"Using default API requirements file: {requirements_file}")

        if requirements_file:
            requirements_path = Path(requirements_file)
            if not requirements_path.is_absolute():
                requirements_path = ROOT / requirements_path
            try:
                requirements = requirements_path.read_text(encoding="utf-8")
            except OSError as error:
                print(f"API requirements file could not be read: {error}")
                return
        else:
            requirements = input("API requirements: ").strip()
        if not requirements:
            print("API requirements are required.")
            return

        contract_ok, contract_message = validate_api_contract(url or "https://rahulshettyacademy.com", requirements)
        if not contract_ok:
            print(contract_message)
            print("Stop: the API contract was not confirmed as JSON-producing endpoints before generation.")
            return

        print("Generating API test cases with Gemini...")
        try:
            plan = generate_test_plan(f"TEST TYPE: API\n{requirements}")
        except RuntimeError as error:
            print(f"API test plan generation failed: {error}")
            return
        snapshot = ""
        url = url or "API requirements"
    GENERATED_TEST_CASES.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_TEST_CASES.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"Generated test cases saved to {GENERATED_TEST_CASES}")
    test_type = plan["test_type"]
    approved_cases = review_generated_test_cases(plan["test_cases"])
    if not approved_cases:
        print("No tests approved. Execution cancelled.")
        return
    approved_requirements = build_requirements_prompt(approved_cases)
    report_path = ROOT / "reports" / f"{test_type}_testing_report.json"
    report_metadata = {
        "test_type": test_type,
        "website_url": url,
        "approved_test_cases": approved_cases,
    }
    test_case = (
        f"TEST TYPE: {test_type.upper()}\n"
        f"APPROVED TEST CASES:\n{approved_requirements}\n\n"
        f"WEBSITE URL:\n{url}\n\nBROWSER DISCOVERY SNAPSHOT:\n{snapshot}"
    )
    print("Generating pytest code with Gemini...")
    try:
        generated_code = generate_test_code(test_case, snapshot, test_type=test_type)
    except RuntimeError as error:
        print(f"Gemini unavailable: {error}")
        return
    GENERATED_TEST.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_TEST.write_text(generated_code, encoding="utf-8")
    automatic_repairs = 0
    repair_history = []
    attempts = []
    execution_attempt = 0
    while True:
        execution_attempt += 1
        try:
            validate_python(GENERATED_TEST)
            collection = run_pytest(GENERATED_TEST, collect_only=True)
            result = collection if collection.exit_code != 0 else run_pytest(GENERATED_TEST)
        except Exception as error:
            result = None
            failure_output = str(error)
        else:
            failure_output = result.traceback

        attempt_status = result.status if result is not None else "failed"
        if repair_history and repair_history[-1]["result"] == "pending":
            repair_history[-1]["result"] = {
                "status": attempt_status,
                "stdout": result.stdout if result is not None else "",
                "stderr": result.stderr if result is not None else failure_output,
            }
        attempts.append({
            "attempt": execution_attempt,
            "status": attempt_status,
            "classification": attempt_status if attempt_status == "skipped" else ("passed" if attempt_status == "passed" else classify_failure(failure_output)),
            "duration": result.duration if result is not None else 0,
            "stdout": result.stdout if result is not None else "",
            "stderr": result.stderr if result is not None else failure_output,
        })
        write_report(report_path, attempts, report_metadata)

        if result is not None:
            print(result.stdout)
            if result.stderr:
                print(result.stderr)
            print(f"Attempt {execution_attempt}: {result.status} ({result.duration:.2f}s)")
            if result.exit_code == 0:
                print(f"Final status: {result.status}")
                return

        repair_number = automatic_repairs + 1
        if automatic_repairs >= 2:
            REPAIR_REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPAIR_REPORT.write_text(json.dumps({
                "website_url": url,
                "test_case": approved_cases,
                "original_test": repair_history[0]["test_code"] if repair_history else GENERATED_TEST.read_text(encoding="utf-8"),
                "repairs": repair_history,
                "final_error": failure_output,
                "possible_cause": classify_failure(failure_output),
                "changes_made": "See each repair entry and generated test history.",
                "current_test_status": "failed",
            }, indent=2), encoding="utf-8")
            print(f"Automatic repair limit reached. Report: {REPAIR_REPORT}")
            if input("Continue with another repair? [y/N]: ").strip().lower() != "y":
                print("Framework paused for human review.")
                return

        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        version = repair_number
        shutil.copy2(GENERATED_TEST, HISTORY_DIR / f"test_case_v{version}.py")
        print(f"Test failed. Sending failure to Gemini for repair {repair_number}...")
        current_code = GENERATED_TEST.read_text(encoding="utf-8")
        repaired_code = repair_test_code(
            test_case,
            snapshot,
            current_code,
            failure_output,
        )
        repair_history.append(create_repair_report_entry(repair_number, current_code, failure_output, "pending"))
        GENERATED_TEST.write_text(repaired_code, encoding="utf-8")
        automatic_repairs += 1


if __name__ == "__main__":
    asyncio.run(main())