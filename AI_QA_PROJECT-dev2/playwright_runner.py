import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
	allure_report_path: str = ""
	report_warning: str = ""

	@property
	def traceback(self) -> str:
		return self.stderr or self.stdout

	@property
	def counts(self) -> dict[str, int]:
		counts = {"passed": 0, "failed": 0, "skipped": 0}
		for count, status in re.findall(r"(\d+)\s+(passed|failed|error|errors|skipped)", self.stdout + "\n" + self.stderr):
			counts["failed" if status in {"error", "errors"} else status] = int(count)
		if not any(counts.values()) and self.exit_code != 0:
			counts["failed"] = 1
		return counts


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
	reports_dir = test_path.parents[1] / "reports"
	allure_results_dir = reports_dir / "allure-results"
	allure_available = importlib.util.find_spec("allure_pytest") is not None
	if collect_only:
		command.append("--collect-only")
	else:
		reports_dir.mkdir(parents=True, exist_ok=True)
		command.extend([
			"--junitxml", str(reports_dir / "test-results.xml"),
			"--html", str(reports_dir / "test-report.html"),
			"--self-contained-html",
		])
		if allure_available:
			command.extend(["--alluredir", str(allure_results_dir), "--clean-alluredir"])

	started = time.perf_counter()
	process_options = {
		"capture_output": True,
		"text": True,
		"stdin": subprocess.DEVNULL,
		"cwd": test_path.parents[2],
	}
	try:
		completed = subprocess.run(command, **process_options)
	except OSError:
		if os.name != "nt":
			raise
		python_command = shutil.which("python") or shutil.which("python3")
		if not python_command:
			raise
		fallback_command = subprocess.list2cmdline([python_command, *command[1:]])
		completed = subprocess.run(fallback_command, shell=True, **process_options)
	duration = time.perf_counter() - started
	allure_report_path = ""
	report_warning = ""
	if not collect_only:
		if not allure_available:
			report_warning = "Allure results were not produced because allure-pytest is not installed."
		else:
			allure_executable = shutil.which("allure")
			if not allure_executable:
				local_allure = test_path.parents[2] / "node_modules" / ".bin" / ("allure.cmd" if os.name == "nt" else "allure")
				if local_allure.is_file():
					allure_executable = str(local_allure)
			if not allure_executable:
				report_warning = "Allure results were saved, but the Allure CLI was not found to create the HTML report."
			else:
				allure_report = reports_dir / "allure-report"
				allure_command = [allure_executable, "generate", str(allure_results_dir), "--clean", "--output", str(allure_report)]
				allure_options = {"capture_output": True, "text": True, "cwd": test_path.parents[2]}
				if os.name == "nt" and Path(allure_executable).suffix.lower() in {".cmd", ".bat"}:
					allure_command = subprocess.list2cmdline(allure_command)
					allure_options["shell"] = True
				try:
					allure_result = subprocess.run(allure_command, **allure_options)
				except OSError as error:
					report_warning = f"Allure HTML report generation failed: {error}"
				else:
					if allure_result.returncode == 0:
						allure_report_path = str(allure_report / "index.html")
					else:
						detail = allure_result.stderr.strip() or allure_result.stdout.strip()
						report_warning = f"Allure HTML report generation failed: {detail or 'Allure CLI exited with an error.'}"
	return TestExecutionResult(
		status=_determine_test_status(completed.returncode, completed.stdout),
		exit_code=completed.returncode,
		duration=duration,
		stdout=completed.stdout,
		stderr=completed.stderr,
		report_path=str(test_path.parents[1] / "reports" / "test-results.xml") if not collect_only else "",
		allure_report_path=allure_report_path,
		report_warning=report_warning,
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
