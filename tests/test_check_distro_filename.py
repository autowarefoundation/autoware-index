"""Tests for scripts/check_distro_filename.py.

`check(paths)` returns 0 when every distributions/<distro>.yaml file declares a
`ros_distro` equal to the filename stem, and 1 when any file fails (mismatch,
missing key, non-dict body, or YAML parse error).
"""

import check_distro_filename
import pytest
import yaml


def _write(dir_path, name, content):
    """Write `content` (str) to dir_path/name and return the str path."""
    p = dir_path / name
    p.write_text(content)
    return str(p)


def _write_distro(dir_path, distro, declared):
    """Create <distro>.yaml whose ros_distro is `declared`. Returns str path."""
    body = yaml.safe_dump({"ros_distro": declared})
    return _write(dir_path, f"{distro}.yaml", body)


# --- happy paths ---------------------------------------------------------


def test_empty_paths_returns_zero():
    assert check_distro_filename.check([]) == 0


def test_single_matching_file_returns_zero(tmp_path):
    path = _write_distro(tmp_path, "humble", "humble")
    assert check_distro_filename.check([path]) == 0


def test_multiple_matching_files_returns_zero(tmp_path):
    paths = [
        _write_distro(tmp_path, "humble", "humble"),
        _write_distro(tmp_path, "jazzy", "jazzy"),
        _write_distro(tmp_path, "rolling", "rolling"),
    ]
    assert check_distro_filename.check(paths) == 0


def test_matching_file_with_extra_keys_returns_zero(tmp_path):
    body = yaml.safe_dump({"ros_distro": "humble", "extra": [1, 2, 3], "nested": {"a": 1}})
    path = _write(tmp_path, "humble.yaml", body)
    assert check_distro_filename.check([path]) == 0


# --- mismatch / failure paths --------------------------------------------


def test_single_mismatching_file_returns_nonzero(tmp_path):
    # ros_distro says "jazzy" but the filename stem is "humble"
    path = _write_distro(tmp_path, "humble", "jazzy")
    assert check_distro_filename.check([path]) == 1


def test_one_bad_among_several_returns_nonzero(tmp_path):
    paths = [
        _write_distro(tmp_path, "humble", "humble"),  # good
        _write_distro(tmp_path, "jazzy", "rolling"),  # BAD: stem jazzy != rolling
        _write_distro(tmp_path, "rolling", "rolling"),  # good
    ]
    assert check_distro_filename.check(paths) == 1


def test_missing_ros_distro_key_returns_nonzero(tmp_path):
    body = yaml.safe_dump({"something_else": "humble"})
    path = _write(tmp_path, "humble.yaml", body)
    assert check_distro_filename.check([path]) == 1


def test_empty_yaml_file_returns_nonzero(tmp_path):
    # An empty document parses to None (not a dict) -> declared is None.
    path = _write(tmp_path, "humble.yaml", "")
    assert check_distro_filename.check([path]) == 1


def test_non_mapping_yaml_returns_nonzero(tmp_path):
    # A top-level list is not a dict -> declared is None.
    path = _write(tmp_path, "humble.yaml", yaml.safe_dump(["humble"]))
    assert check_distro_filename.check([path]) == 1


def test_scalar_yaml_returns_nonzero(tmp_path):
    # A bare scalar document is not a dict -> declared is None.
    path = _write(tmp_path, "humble.yaml", "humble\n")
    assert check_distro_filename.check([path]) == 1


def test_yaml_parse_error_returns_nonzero(tmp_path):
    # Unbalanced bracket / bad indentation triggers a yaml.YAMLError.
    path = _write(tmp_path, "humble.yaml", "ros_distro: [unterminated\n")
    assert check_distro_filename.check([path]) == 1


# --- stem semantics ------------------------------------------------------


def test_stem_strips_only_final_extension(tmp_path):
    # Path.stem of "humble.yaml" is "humble"; ros_distro must equal that.
    path = _write_distro(tmp_path, "humble", "humble.yaml")
    # ros_distro is "humble.yaml" but stem is "humble" -> mismatch.
    assert check_distro_filename.check([path]) == 1


def test_dotted_filename_stem(tmp_path):
    # File named "ros2.humble.yaml" has stem "ros2.humble".
    body = yaml.safe_dump({"ros_distro": "ros2.humble"})
    path = _write(tmp_path, "ros2.humble.yaml", body)
    assert check_distro_filename.check([path]) == 0


def test_declared_value_must_match_exactly(tmp_path):
    # Trailing whitespace makes it a different string -> mismatch.
    path = _write_distro(tmp_path, "humble", "humble ")
    assert check_distro_filename.check([path]) == 1


# --- diagnostics ---------------------------------------------------------


def test_mismatch_message_written_to_stderr(tmp_path, capsys):
    path = _write_distro(tmp_path, "humble", "jazzy")
    rc = check_distro_filename.check([path])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ros_distro is 'jazzy'" in err
    assert "stem is 'humble'" in err


def test_parse_error_message_written_to_stderr(tmp_path, capsys):
    path = _write(tmp_path, "humble.yaml", "ros_distro: [unterminated\n")
    rc = check_distro_filename.check([path])
    assert rc == 1
    err = capsys.readouterr().err
    assert "YAML parse error" in err


def test_all_good_writes_nothing_to_stderr(tmp_path, capsys):
    path = _write_distro(tmp_path, "humble", "humble")
    rc = check_distro_filename.check([path])
    assert rc == 0
    assert capsys.readouterr().err == ""


# --- accumulation: every bad file is reported ----------------------------


def test_multiple_failures_all_reported(tmp_path, capsys):
    paths = [
        _write_distro(tmp_path, "humble", "jazzy"),  # bad
        _write_distro(tmp_path, "rolling", "iron"),  # bad
    ]
    rc = check_distro_filename.check(paths)
    assert rc == 1
    err = capsys.readouterr().err
    # The loop does not short-circuit; both mismatches are diagnosed.
    assert "humble" in err
    assert "rolling" in err
