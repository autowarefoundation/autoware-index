"""Tests for scripts/build_report.py (the build job summary).

The summary is the first thing a reader opens when a build check goes red, so
each way a package can fail must reach it with its cause, not only the
package name. Each test lays out the .sweep-* files that the build steps leave
for one failure mode and checks the headline, the excerpt, and the
annotation.
"""

from pathlib import Path

import build_report
import pytest
import yaml

# The output of a real failure: autoware_latent_drive at 1.0.1 on Jazzy,
# https://github.com/autowarefoundation/autoware-index/actions/runs/41093229072
CMAKE_FAILURE_LOG = """\
Starting >>> autoware_latent_drive
--- output: autoware_latent_drive
-- The CXX compiler identification is GNU 13.3.0
-- Found tf2_ros: 0.36.23 (/opt/ros/jazzy/share/tf2_ros/cmake)
CMake Error at CMakeLists.txt:16 (find_package):
  By not providing "FindOpenCV.cmake" in CMAKE_MODULE_PATH this project has
  asked CMake to find a package configuration file provided by "OpenCV", but
  CMake did not find one.

  Could not find a package configuration file provided by "OpenCV" with any
  of the following names:

    OpenCVConfig.cmake
    opencv-config.cmake

-- Configuring incomplete, errors occurred!
---
--- stderr: autoware_latent_drive
CMake Error at CMakeLists.txt:16 (find_package):
---
Failed   <<< autoware_latent_drive [2.01s, exited with code 1]

Summary: 0 packages finished [2.13s]
  1 package failed: autoware_latent_drive
"""

ROSDEP_LOG = """\
ERROR: the following packages/stacks could not have their rosdep keys resolved
to system dependencies:
vision_pilot: Cannot locate rosdep definition for [opencv]
"""


def layout(tmp_path: Path, **files: str) -> Path:
    """Write .sweep-* files; the keyword `sweep_build_log` becomes .sweep-build.log."""
    for name, text in files.items():
        *stem, suffix = name.split("_")
        (tmp_path / f".{'-'.join(stem)}.{suffix}").write_text(text)
    return tmp_path


def run(tmp_path: Path, steps: str = "{}", logs_url: str = ""):
    return build_report.report(
        tmp_path, "awesome_tools", "tag", "1.2.0", steps, logs_url, "https://example.com/run/42"
    )


def test_own_cmake_error_reaches_the_headline_excerpt_and_annotation(tmp_path):
    layout(
        tmp_path,
        sweep_present_txt="autoware_latent_drive\n",
        sweep_results_txt="autoware_latent_drive failure skipped\n",
        sweep_reasons_txt="autoware_latent_drive\tfailed\tautoware_latent_drive\n",
        sweep_build_log=CMAKE_FAILURE_LOG,
    )
    summary, annotations, code = run(tmp_path, logs_url="https://example.com/artifact/7")
    assert code == 1
    assert (
        '**1 of 1 packages failed:** autoware_latent_drive (CMake: package "OpenCV" not found)'
        in summary
    )
    assert (
        '| autoware_latent_drive | yes | failure | CMake: package "OpenCV" not found |' in summary
    )
    assert "<details><summary>autoware_latent_drive:" in summary
    assert "CMake Error at CMakeLists.txt:16 (find_package):" in summary
    assert "[build logs artifact](https://example.com/artifact/7)" in summary
    assert annotations == [
        '::error title=autoware_latent_drive build failed::CMake: package "OpenCV" not found'
    ]


def test_compiler_error_is_the_headline(tmp_path):
    log = (
        "--- output: pkg_a\n"
        "[ 50%] Building CXX object CMakeFiles/a.dir/a.cpp.o\n"
        "/src/a.cpp:3:10: fatal error: foo.hpp: No such file or directory\n"
        "---\n"
    )
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_reasons_txt="pkg_a\tfailed\tpkg_a\n",
        sweep_build_log=log,
    )
    summary, annotations, _ = run(tmp_path)
    assert "/src/a.cpp:3:10: fatal error: foo.hpp: No such file or directory" in annotations[0]
    assert "Building CXX object" not in summary


def test_failed_dependency_blames_the_dependency_with_its_cause(tmp_path):
    log = "--- output: pkg_dep\n/src/d.cpp:1:1: error: boom\n---\n"
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_reasons_txt="pkg_a\tfailed\tpkg_dep\n",
        sweep_build_log=log,
    )
    summary, annotations, _ = run(tmp_path)
    assert "blocked by `pkg_dep`, which failed: /src/d.cpp:1:1: error: boom" in summary
    assert annotations[0].startswith("::error title=pkg_a build failed::blocked by `pkg_dep`")


def test_rosdep_key_is_the_cause(tmp_path):
    layout(
        tmp_path,
        sweep_present_txt="vision_pilot\n",
        sweep_results_txt="vision_pilot failure skipped\n",
        sweep_reasons_txt="vision_pilot\tfailed\tvision_pilot\n",
        sweep_unresolved_txt="vision_pilot opencv\n",
        sweep_rosdep_log=ROSDEP_LOG,
    )
    summary, _, _ = run(tmp_path)
    assert "rosdep cannot resolve key(s): opencv" in summary
    assert "vision_pilot: Cannot locate rosdep definition for [opencv]" in summary


def test_dependency_missing_from_the_index(tmp_path):
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_reasons_txt="pkg_a\tmissing\tpkg_dep\n",
    )
    summary, _, _ = run(tmp_path)
    assert "index dependency `pkg_dep` is not in the imported source tree" in summary


def test_dependency_not_installed(tmp_path):
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_reasons_txt="pkg_a\tnot-installed\tpkg_dep\n",
    )
    summary, _, _ = run(tmp_path)
    assert "dependency `pkg_dep` has no install directory" in summary


def test_absent_package_says_why(tmp_path):
    layout(tmp_path, sweep_present_txt="", sweep_absent_txt="pkg_gone\n")
    summary, annotations, code = run(tmp_path)
    assert code == 1
    assert "| pkg_gone | **no** | - | not found in the repository tree at this ref |" in summary
    assert annotations == [
        "::error title=pkg_gone not found::not found in the repository tree at this ref"
    ]


def test_pipeline_fault_names_the_step_and_blames_no_package(tmp_path):
    layout(tmp_path, sweep_present_txt="pkg_a\n")
    steps = '{"scan": {"conclusion": "success"}, "rosdep": {"conclusion": "failure"}}'
    summary, annotations, code = run(tmp_path, steps=steps)
    assert code == 0
    assert annotations == []
    assert "The step `Install dependencies (rosdep)` did not finish" in summary
    assert "[run log](https://example.com/run/42)" in summary
    assert "| pkg_a | yes | - | inconclusive: no verdict |" in summary
    assert "**1 of 1 packages have no verdict.**" in summary
    assert "All 1 packages built" not in summary


@pytest.mark.parametrize(
    "steps",
    [
        # A new push to the PR cancels the running build.
        '{"build": {"conclusion": "cancelled"}}',
        # A step without an id is not in toJSON(steps) at all.
        "{}",
    ],
)
def test_a_package_without_a_verdict_is_never_reported_as_built(tmp_path, steps):
    layout(
        tmp_path, sweep_present_txt="pkg_a\npkg_b\n", sweep_results_txt="pkg_a success skipped\n"
    )
    summary, _, code = run(tmp_path, steps=steps)
    assert code == 0
    assert "**1 of 2 packages have no verdict.**" in summary
    assert "packages built" not in summary


def test_a_failed_logs_upload_is_not_a_pipeline_fault(tmp_path):
    layout(tmp_path, sweep_present_txt="pkg_a\n", sweep_results_txt="pkg_a success skipped\n")
    steps = '{"logs": {"outcome": "failure", "conclusion": "success"}}'
    summary, _, _ = run(tmp_path, steps=steps)
    assert "pipeline fault" not in summary
    assert "**All 1 packages built.**" in summary


def test_step_names_match_the_workflow(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/build-repository.yaml").read_text())
    steps = {s["id"]: s["name"] for s in workflow["jobs"]["build"]["steps"] if "id" in s}
    for step_id, name in build_report.STEP_NAMES.items():
        assert steps.get(step_id) == name, step_id
    # Every step that can leave packages without a verdict is named.
    assert set(steps) - set(build_report.STEP_NAMES) == {"handoff", "logs"}


def test_compiler_text_is_escaped_for_html(tmp_path):
    log = "--- output: pkg_a\n/src/a.cpp:1:1: error: no match for std::vector<int> | x\n---\n"
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_build_log=log,
    )
    summary, annotations, _ = run(tmp_path)
    assert (
        "<summary>pkg_a: /src/a.cpp:1:1: error: no match for std::vector&lt;int&gt; \\| x</summary>"
        in summary
    )
    assert "std::vector<int>" in summary  # the fenced excerpt stays verbatim
    assert "std::vector<int>" in annotations[0]


def test_all_green(tmp_path):
    layout(tmp_path, sweep_present_txt="pkg_a\n", sweep_results_txt="pkg_a success skipped\n")
    summary, annotations, code = run(tmp_path)
    assert (code, annotations) == (0, [])
    assert "**All 1 packages built.**" in summary
    assert "| pkg_a | yes | success |  |" in summary


def test_no_error_line_falls_back_to_the_log_tail(tmp_path):
    lines = [f"line {i}" for i in range(100)]
    log = "--- output: pkg_a\n" + "\n".join(lines) + "\n---\n"
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_build_log=log,
    )
    summary, _, _ = run(tmp_path)
    assert "line 99" in summary
    assert "line 39\n" not in summary
    assert "line 40\n" in summary


def test_colcon_package_log_is_the_fallback(tmp_path):
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_reasons_txt="pkg_a\tfailed\tpkg_a\n",
    )
    path = tmp_path / "log" / "latest_build" / "pkg_a"
    path.mkdir(parents=True)
    (path / "stdout_stderr.log").write_text("/src/a.cpp:1:1: error: from the colcon log\n")
    _, annotations, _ = run(tmp_path)
    assert "from the colcon log" in annotations[0]


def test_excerpt_is_capped_and_fenced_safely(tmp_path):
    log = "--- output: pkg_a\nerror: ```\n" + "x" * 50_000 + "\n---\n"
    layout(
        tmp_path,
        sweep_present_txt="pkg_a\n",
        sweep_results_txt="pkg_a failure skipped\n",
        sweep_build_log=log,
    )
    summary, _, _ = run(tmp_path)
    assert len(summary.encode()) < build_report.BLOCK_BYTES + 2_000
    assert "````text" in summary


def test_annotations_stay_under_the_per_step_limit(tmp_path):
    pkgs = [f"pkg_{i:02}" for i in range(15)]
    layout(
        tmp_path,
        sweep_present_txt="".join(f"{p}\n" for p in pkgs),
        sweep_results_txt="".join(f"{p} failure skipped\n" for p in pkgs),
    )
    _, annotations, _ = run(tmp_path)
    assert len(annotations) == build_report.MAX_ANNOTATIONS
    assert annotations[-1] == "::error::6 more packages failed; see the job summary"


@pytest.mark.parametrize(
    "text,expected",
    [("a:b,c", "a%3Ab%2Cc"), ("50%\nnext", "50%25%0Anext")],
)
def test_annotation_properties_are_escaped(text, expected):
    assert build_report.escape_property(text) == expected
