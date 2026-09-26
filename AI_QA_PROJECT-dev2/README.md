# AI-Powered Self-Healing Test Automation Framework

## 1. Project Summary

This project is an AI-assisted test automation framework for web UI and REST API testing.
It converts testing requirements and live application context into executable pytest tests.
The framework can validate generated tests, execute them, repair failures with Gemini, and
produce test reports.

The project combines:

- Gemini for test-case generation and test repair.
- Playwright MCP for live browser discovery.
- Playwright for UI test execution.
- `requests` for API test execution.
- pytest for validation and execution.
- JSON, HTML, and JUnit reports for results.

## 2. Problem Solved

Traditional automation requires a QA engineer to manually inspect the application, write
selectors or API requests, maintain test code, repair failures, and prepare reports.

This framework reduces that effort by creating a pipeline that can:

1. Discover a live website.
2. Generate test cases with AI.
3. Allow a human to approve the cases.
4. Generate executable Python tests.
5. Run the tests with pytest.
6. Send failures back to Gemini for repair.
7. Retry within a configured limit.
8. Save the complete execution history.

## 3. High-Level Architecture

```text
User configuration and test mode
              |
              v
                main.py
        Orchestration layer
        /            \
       /              \
      v                v
 UI mode              API mode
      |                |
      v                v
 Playwright MCP       API requirements
 browser discovery    and API context
      |                |
      +-------+--------+
              v
                                                                                  Gemini AI client
                                                                          ai_client.py
              |
              v
     Generated test cases
              |
              v
       Human approval
              |
              v
   generated/tests/test_case.py
              |
              v
     Python validation and pytest
              |
       +------+------+
       |             |
     PASS          FAIL
       |             |
       v             v
   Reports       Save failed version
                     |
                     v
              Gemini self-healing
                     |
                     v
                  Retry
```

## 4. Main Components

### `main.py` - Entry point

Controls the complete workflow:

- Selects UI or API mode.
- Loads the website URL or API requirements.
- Starts MCP discovery for UI mode.
- Requests test cases from Gemini.
- Displays test cases for human approval.
- Generates the pytest source file.
- Validates and executes the generated tests.
- Starts bounded repair attempts after failures.
- Writes generated test cases and per-attempt JSON reports under `generated/`.

### Active Python modules

The `main.py` entry point currently imports these top-level modules directly:

- `ai_client.py`: Gemini test generation and repair.
- `mcp_client.py`: Playwright MCP discovery.
- `playwright_runner.py`: pytest execution and HTML, JUnit, and Allure reporting.

`qa_app/` is not currently wired into the entry point.

### `mcp_client.py` - Browser Discovery

Provides live browser context for UI testing:

- Starts Playwright MCP through `npx`.
- Opens the configured website.
- Logs in with the approved test account.
- Preserves the authenticated browser session.
- Discovers same-application pages and SPA routes.
- Captures browser snapshots for Gemini.
- Avoids unrelated pages outside the application path.

### `ai_client.py` - Gemini Integration

Handles communication with Gemini:

- Generates UI test cases from browser discovery.
- Generates API test cases from API requirements.
- Generates Playwright code for UI tests.
- Generates `requests` code for API tests.
- Repairs failed UI and API tests.
- Removes Markdown code fences from responses.
- Validates generated test-plan structure.
- Retries temporary Gemini server errors.

### `playwright_runner.py` - Execution and Reporting

Provides mode-independent test execution:

- Compiles generated Python.
- Confirms that a `test_...` function exists.
- Runs pytest once so execution and collection failures share the same report path.
- Captures output, errors, status, and duration.
- Creates HTML, JUnit, and Allure reports.
- Classifies common failures.
- Writes JSON attempt reports.

### `generated/` - Generated Artifacts

Contains AI-created test output:

- `generated/generated_test_cases.json`: AI-generated test plan saved before approval.
- `generated/tests/test_case.py`: latest generated pytest file.
- `generated/tests/history/`: previous versions saved before repairs.

### `generated/reports/` - Results

Contains execution evidence produced by each run:

- `ui_testing_report.json`: UI attempts and metadata.
- `api_testing_report.json`: API attempts and metadata.
- `test-report.html`: human-readable pytest report.
- `test-results.xml`: JUnit report for CI tools.
- `allure-results/` and `allure-report/index.html`: Allure result data and HTML report.
- `repair_report.json`: repair history and final failure information.

The generated HTML, JUnit XML, JSON, and repair reports are intentionally kept
under `generated/reports/` so generated output has one predictable location.

### `tests/` - Framework Tests

Contains tests for framework behavior, such as test-type detection, plan parsing,
requirement formatting, and human approval logic.

## 5. UI Testing Flow

UI testing starts from a website URL. It can optionally use a requirements file
configured with `UI_REQUIREMENTS_FILE`; if not configured, the project uses
`ui_requirements.md` when that file exists. Otherwise, it retains discovery-only
test generation.

Requirements-driven UI files use one uniquely identified requirement per line:

```text
REQ-001: A signed-in user can search products by name.
REQ-002: A signed-in user can add a product to the cart.
```

Generated test cases carry the matching `requirement_id`. The planner checks
that every supplied ID has test coverage and rejects missing or unknown IDs.

Example `.env` setting:

```env
UI_REQUIREMENTS_FILE=ui_requirements.md
```

```text
TEST_MODE=ui
WEBSITE_URL=target website
        |
        v
MCP opens the browser
        |
        v
MCP authenticates with TEST_EMAIL and TEST_PASSWORD
        |
        v
MCP discovers application pages and routes
        |
        v
Browser snapshots are sent to Gemini
        |
        v
Gemini creates UI test cases
        |
        v
User approves test cases
        |
        v
Gemini creates Playwright pytest code
        |
        v
pytest executes the generated tests
```

Typical UI scenarios include:

- Login success and validation.
- Product listing and filtering.
- Product visibility.
- Add to cart.
- Cart validation.
- Checkout navigation.
- Orders page.
- Logout.

## 6. API Testing Flow

API testing starts from API requirements and an API base URL. It does not need browser
or MCP discovery.

```text
TEST_MODE=api
API_BASE_URL=API base URL
API_REQUIREMENTS_FILE=requirements file
        |
        v
Gemini reads API requirements
        |
        v
Gemini creates API test cases
        |
        v
User approves test cases
        |
        v
Gemini creates requests pytest code
        |
        v
pytest executes API tests
```

API tests should validate:

- HTTP methods.
- Status codes.
- Authentication responses.
- Runtime tokens.
- JSON response fields.
- Product, cart, order, and order-history behavior.

## 7. Human Approval Step

The framework does not execute generated scenarios immediately. It displays a review menu:

```text
[a] approve all
[p] approve specific
[s] reject specific
[m] add manual test
[r] reject all
[e] execute approved
```

This is important because AI-generated tests must be reviewed before execution, especially
for critical workflows.

## 8. Self-Healing Workflow

When generated code fails:

1. pytest captures the failure output.
2. The framework classifies the failure.
3. The current generated test is saved in `generated/tests/history/`.
4. Gemini receives the requirements, current code, browser/API context, and failure.
5. Gemini returns repaired Python code.
6. The repaired code is validated and executed again.
7. The process stops when the test passes or the repair limit is reached.

Example:

```text
Generated locator fails
        |
        v
Failure captured
        |
        v
Gemini repairs locator
        |
        v
Test runs again
        |
        +--> PASS: finish
        |
        +--> FAIL: save version and retry
```

Self-healing is bounded by `MAX_REPAIR_ATTEMPTS`; it does not retry forever.

## 9. Configuration

Keep secrets only in `.env`. Do not commit or share the file.

### UI configuration

```env
TEST_MODE=ui
WEBSITE_URL=https://rahulshettyacademy.com/client/
TEST_EMAIL=approved_test_email
TEST_PASSWORD=approved_test_password
GEMINI_API_KEY=your_api_key
GEMINI_MODEL=gemini-3.5-flash-lite
MAX_REPAIR_ATTEMPTS=3
MIN_GENERATED_TEST_CASES=8
DISCOVERY_MAX_PAGES=100
```

### API configuration

```env
TEST_MODE=api
API_BASE_URL=https://your-api.example.com
API_REQUIREMENTS_FILE=test_cases/api_requirements.txt
GEMINI_API_KEY=your_api_key
GEMINI_MODEL=gemini-3.5-flash-lite
MAX_REPAIR_ATTEMPTS=3
```

Never place real keys, passwords, tokens, or private test data in this document.

## 10. How To Run The Project

From the project directory:

```cmd
python -m pip install -r requirements.txt
npm install
npm install --global allure-commandline
C:\Users\Jatin\AppData\Local\Python\bin\python.exe main.py
```

Allure HTML generation also requires Java. Reports are written only after a test
execution starts; the generated test-case plan is saved separately to
`generated/generated_test_cases.json` before approval.

For UI mode, approve and execute all generated cases:

```text
a
e
```

The framework then generates tests, runs pytest, and creates reports.

## 11. Validation Commands

Run the framework tests:

```cmd
C:\Users\Jatin\AppData\Local\Python\bin\python.exe -m pytest tests/test_api_mode.py -q
```

Compile the main modules:

```cmd
C:\Users\Jatin\AppData\Local\Python\bin\python.exe -m py_compile main.py ai_client.py mcp_client.py playwright_runner.py
```

Use the pytest summary to confirm that all collected framework tests passed.

### Pre-commit checks

Install the development tools and enable checks before each Git commit:

```cmd
python -m pip install -r requirements-dev.txt
python -m pre_commit install
```

Run the checks manually across tracked files:

```cmd
python -m pre_commit run --all-files
```

Generated reports and test artifacts are excluded from these checks.

## 12. Presentation Demonstration Script

Use this order during the presentation:

1. Introduce the problem: manual test creation and maintenance are slow.
2. Show the project folders and explain the four main Python components.
3. Explain that `main.py` is the orchestrator.
4. Explain that MCP supplies live browser context for UI testing.
5. Explain that Gemini generates test cases and executable code.
6. Run UI mode with the Rahul Shetty practice site.
7. Show authenticated discovery and generated test cases.
8. Approve the cases with `a`, then execute with `e`.
9. Show pytest results and the final status.
10. Explain that a failed locator can trigger Gemini self-healing.
11. Open `generated/reports/repair_report.json` and explain the repair history.
12. Explain API mode and show that it uses `requests`, not Playwright.
13. Show HTML, JSON, and JUnit reports.
14. Finish with limitations and future improvements.

.

## 14. Strengths

- Supports both UI and API testing.
- Uses live browser context for UI locators.
- Requires human approval before execution.
- Validates generated code before running it.
- Automatically repairs many selector and assertion failures.
- Preserves repair history.
- Produces standard test reports.
- Keeps credentials in environment variables.

## 15. Limitations To Explain Honestly

- Gemini output still requires human review.
- Protected pages require valid approved credentials.
- API mode requires a real API base URL and valid API requirements.
- CAPTCHA, OTP, MFA, and payment integrations may block automation.
- MCP discovers reachable routes and controls; it cannot guarantee every hidden
  application state.
- Gemini quota, model availability, and network access affect execution.
- Self-healing can improve a test but cannot guarantee business correctness.

## 16. Definition Of Done

The project demonstration is successful when:

- UI or API mode can be selected.
- Requirements or live browser context are processed.
- Gemini creates reviewable test cases.
- The user approves the cases.
- Valid pytest code is generated.
- Tests are collected and executed.
- Failures are classified.
- Repairs are bounded and recorded.
- Reports are created.
- Secrets remain outside source code and documentation.
