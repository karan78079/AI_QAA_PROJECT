import subprocess

import playwright_runner
from playwright_runner import run_pytest


def test_runner_uses_current_python_and_writes_reports_under_generated(tmp_path):
	test_dir = tmp_path / "generated" / "tests"
	test_dir.mkdir(parents=True)
	test_file = test_dir / "test_sample.py"
	test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")

	result = run_pytest(test_file)

	reports_dir = tmp_path / "generated" / "reports"
	assert result.exit_code == 0
	assert result.counts == {"passed": 1, "failed": 0, "skipped": 0}
	assert result.report_path == str(reports_dir / "test-results.xml")
	assert (reports_dir / "test-results.xml").exists()
	assert (reports_dir / "test-report.html").exists()


def test_runner_writes_reports_when_collection_fails(tmp_path):
	test_dir = tmp_path / "generated" / "tests"
	test_dir.mkdir(parents=True)
	test_file = test_dir / "test_broken.py"
	test_file.write_text("raise RuntimeError('collection failed')\n", encoding="utf-8")

	result = run_pytest(test_file)
	reports_dir = tmp_path / "generated" / "reports"

	assert result.exit_code != 0
	assert (reports_dir / "test-results.xml").exists()
	assert (reports_dir / "test-report.html").exists()


def test_runner_falls_back_to_path_python_when_selected_executable_cannot_launch(tmp_path, monkeypatch):
	test_dir = tmp_path / "generated" / "tests"
	test_dir.mkdir(parents=True)
	test_file = test_dir / "test_sample.py"
	test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
	calls = []

	def fake_run(command, **options):
		calls.append((command, options))
		if len(calls) == 1:
			raise OSError(193, "not a valid Win32 application")
		return subprocess.CompletedProcess(command, 0, "1 passed in 0.01s\n", "")

	monkeypatch.setattr(playwright_runner.sys, "executable", "invalid-python.exe")
	monkeypatch.setattr(playwright_runner.shutil, "which", lambda _: r"C:\Python\python.exe")
	monkeypatch.setattr(playwright_runner.subprocess, "run", fake_run)
	monkeypatch.setattr(playwright_runner.importlib.util, "find_spec", lambda _: None)

	result = run_pytest(test_file)

	assert result.exit_code == 0
	assert len(calls) == 2
	assert calls[1][1]["shell"] is True