"""Tests for scripts/test_report.py (the test job summary).

A red test check must say which test cases failed and how, not only that the
package's tests failed. Each test lays out a _result/result.json and the
JUnit files or logs that colcon test leaves, then checks the summary and the
annotations.
"""

import json
from pathlib import Path

import test_report
import yaml

GTEST_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="3" failures="1">
  <testsuite name="TestPlanner" tests="3" failures="1">
    <testcase name="plans" classname="TestPlanner"/>
    <testcase name="stops" classname="TestPlanner"/>
    <testcase name="avoids" classname="TestPlanner">
      <failure message="test_planner.cpp:42&#10;Expected equality of these values:&#10;  path.size()&#10;    Which is: 0&#10;  3"><![CDATA[test_planner.cpp:42
Expected equality of these values:
  path.size()
    Which is: 0
  3]]></failure>
    </testcase>
  </testsuite>
</testsuites>
"""

PYTEST_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="2" errors="1">
  <testcase classname="test.test_node" name="test_ok"/>
  <testcase classname="test.test_node" name="test_launch">
    <error message="RuntimeError: node died">Traceback ...</error>
  </testcase>
</testsuite></testsuites>
"""


def result(tmp_path: Path, packages: dict[str, tuple[str | None, str | None]]) -> None:
    """Write _result/result.json; each package maps to (build, test) outcomes."""
    directory = tmp_path / "_result"
    directory.mkdir()
    data = {
        "repo_name": "awesome_tools",
        "packages": {
            pkg: {"present": build is not None, "build_outcome": build, "test_outcome": test}
            for pkg, (build, test) in packages.items()
        },
    }
    (directory / "result.json").write_text(json.dumps(data))


def junit(tmp_path: Path, pkg: str, name: str, text: str) -> None:
    path = tmp_path / "build" / pkg / "test_results" / pkg
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)


def run(tmp_path: Path, steps: str = "{}", logs_url: str = ""):
    return test_report.report(tmp_path, steps, logs_url, "https://example.com/run/42")


def test_gtest_failure_names_the_case_and_its_message(tmp_path):
    result(tmp_path, {"pkg_a": ("success", "failure")})
    junit(tmp_path, "pkg_a", "test_planner.gtest.xml", GTEST_XML)
    summary, annotations, code = run(tmp_path, logs_url="https://example.com/artifact/7")
    assert code == 1
    assert "**1 of 1 tested packages failed:** pkg_a" in summary
    assert (
        "| pkg_a | failure | 1 of 3 test cases failed: `TestPlanner.avoids`"
        " (first: test_planner.cpp:42) |" in summary
    )
    assert "TestPlanner.avoids\n  test_planner.cpp:42\n  Expected equality" in summary
    assert summary.count("Which is: 0") == 1
    assert "[test logs artifact](https://example.com/artifact/7)" in summary
    assert annotations == [
        "::error title=pkg_a tests failed::1 of 3 test cases failed: `TestPlanner.avoids`"
        " (first: test_planner.cpp:42)"
    ]


def test_pytest_error_counts_as_a_failed_case(tmp_path):
    result(tmp_path, {"pkg_a": ("success", "failure")})
    (tmp_path / "build" / "pkg_a").mkdir(parents=True)
    (tmp_path / "build" / "pkg_a" / "pytest.xml").write_text(PYTEST_XML)
    summary, _, _ = run(tmp_path)
    assert "1 of 2 test cases failed: `test.test_node.test_launch`" in summary
    assert "(first: RuntimeError: node died)" in summary
    assert "  Traceback ..." in summary


def test_xml_that_is_not_junit_is_ignored(tmp_path):
    result(tmp_path, {"pkg_a": ("success", "failure")})
    junit(tmp_path, "pkg_a", "test_planner.gtest.xml", GTEST_XML)
    (tmp_path / "build" / "pkg_a" / "package.xml").write_text("<package><name>a</name></package>")
    (tmp_path / "build" / "pkg_a" / "broken.xml").write_text("<testsuite")
    summary, _, _ = run(tmp_path)
    assert "1 of 3 test cases failed" in summary


def test_failure_without_a_failed_case_falls_back_to_the_newest_log(tmp_path):
    result(tmp_path, {"pkg_a": ("success", "failure")})
    for stamp, text in (("2026-10-30_10-00-00", "old run"), ("2026-10-30_11-00-00", "segfault")):
        path = tmp_path / "log" / f"test_{stamp}" / "pkg_a"
        path.mkdir(parents=True)
        (path / "stdout_stderr.log").write_text(f"{text}\n")
    summary, annotations, _ = run(tmp_path)
    assert "no failed case is in the JUnit results" in annotations[0]
    assert "segfault" in summary
    assert "old run" not in summary


def test_many_failed_cases_are_listed_up_to_a_limit(tmp_path):
    cases = "".join(
        f'<testcase classname="T" name="c{i}"><failure message="m{i}"/></testcase>'
        for i in range(8)
    )
    result(tmp_path, {"pkg_a": ("success", "failure")})
    junit(tmp_path, "pkg_a", "t.xml", f"<testsuite>{cases}</testsuite>")
    _, annotations, _ = run(tmp_path)
    assert "8 of 8 test cases failed: `T.c0`, `T.c1`, `T.c2`, `T.c3`, `T.c4` and 3 more" in (
        annotations[0]
    )


def test_missing_outcome_is_inconclusive_and_names_the_failed_step(tmp_path):
    result(tmp_path, {"pkg_a": ("success", None)})
    steps = '{"cache": {"conclusion": "failure"}}'
    summary, annotations, code = run(tmp_path, steps=steps)
    assert code == 1
    assert annotations == []
    assert "The step `Restore built workspace` (failure) did not finish" in summary
    assert "**1 of 1 tested packages have no outcome.**" in summary
    assert "| pkg_a | inconclusive | no test outcome |" in summary
    assert "passed" not in summary


def test_all_passed_and_not_built_rows(tmp_path):
    result(tmp_path, {"pkg_a": ("success", "success"), "pkg_b": ("failure", None)})
    summary, annotations, code = run(tmp_path)
    assert (code, annotations) == (0, [])
    assert "**All 1 tested packages passed.**" in summary
    assert "| pkg_b | skipped (not built) |  |" in summary


def test_annotations_stay_under_the_per_step_limit(tmp_path):
    pkgs = {f"pkg_{i:02}": ("success", "failure") for i in range(12)}
    result(tmp_path, pkgs)
    _, annotations, _ = run(tmp_path)
    assert len(annotations) == test_report.MAX_ANNOTATIONS
    assert annotations[-1] == "::error::3 more packages failed tests; see the job summary"


def test_step_names_match_the_workflow(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/test-repository.yaml").read_text())
    steps = {s["id"]: s["name"] for s in workflow["jobs"]["test"]["steps"] if "id" in s}
    for step_id, name in test_report.STEP_NAMES.items():
        assert steps.get(step_id) == name, step_id
    assert set(steps) - set(test_report.STEP_NAMES) == {"logs"}


def test_the_module_defines_no_names_that_pytest_would_collect():
    """scripts/test_report.py is imported here; a test_* function in it would run as a test."""
    assert not [name for name in vars(test_report) if name.startswith(("test_", "Test"))]
