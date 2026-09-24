import os
import subprocess
import sys
import time
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestExecutionResult:
	status: str
	exit_code: int
	duration: float
	stdout: str
	stderr: str
	report_path: str = ""

	@property
	def traceback(self) -> str:
		return self.stderr or self.stdout


def _determine_test_status(returncode: int, output: str) -> str:
	if returncode != 0:
		return "failed"
	if " skipped" in output or " skipped in " in output:
		return "skipped"
	return "passed"


def _result_status(returncode: int, output: str) -> str:
	return _determine_test_status(returncode, output)


def validate_generated_python_test(test_file: Path) -> None:
	source = test_file.read_text(encoding="utf-8")
	compile(source, str(test_file), "exec")
	if "def test_" not in source:
		raise ValueError("Generated test does not contain a pytest test function")


def validate_python(test_file: Path) -> None:
	validate_generated_python_test(test_file)


def execute_pytest_for_file(test_file: Path, collect_only: bool = False) -> TestExecutionResult:
	test_path = test_file.resolve()
	command = [sys.executable, "-m", "pytest", str(test_path)]
	if collect_only:
		command.append("--collect-only")
	else:
		reports_dir = test_path.parents[2] / "reports"
		reports_dir.mkdir(parents=True, exist_ok=True)
		command.extend([
			"--junitxml", str(reports_dir / "test-results.xml"),
			"--html", str(reports_dir / "test-report.html"),
			"--self-contained-html",
		])

	started = time.perf_counter()
	process_command = command
	process_options = {}
	if os.name == "nt":
		process_command = subprocess.list2cmdline(["python", *command[1:]])
		process_options["shell"] = True
	completed = subprocess.run(
		process_command,
		capture_output=True,
		text=True,
		cwd=test_path.parents[2],
		**process_options,
	)
	duration = time.perf_counter() - started
	return TestExecutionResult(
		status=_determine_test_status(completed.returncode, completed.stdout),
		exit_code=completed.returncode,
		duration=duration,
		stdout=completed.stdout,
		stderr=completed.stderr,
		report_path=str(test_path.parents[2] / "reports" / "test-results.xml") if not collect_only else "",
	)


def run_pytest(test_file: Path, collect_only: bool = False) -> TestExecutionResult:
	return execute_pytest_for_file(test_file, collect_only=collect_only)


def classify_test_failure(output: str) -> str:
	text = output.lower()
	if any(value in text for value in (
		"connection_timed_out",
		"connection refused",
		"err_connection_refused",
		"network_changed",
		"net::err_",
		"server at http://",
		"not reachable",
	)):
		return "blocked_network"
	if "no tests ran" in text or "skipped" in text:
		return "blocked_environment"
	if "quota" in text or "rate limit" in text or "429" in text:
		return "blocked_ai_quota"
	if "credential" in text or "unauthorized" in text:
		return "blocked_credentials"
	return "failed_test"


def classify_failure(output: str) -> str:
	return classify_test_failure(output)


def write_test_report(path: Path, attempts: list[dict], metadata: dict | None = None) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	report = {"attempts": attempts}
	if metadata:
		report.update(metadata)
	path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def write_report(path: Path, attempts: list[dict], metadata: dict | None = None) -> None:
	write_test_report(path, attempts, metadata=metadata)
