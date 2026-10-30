"""Regressions for PR #8 review 5713648402."""

import pytest
from test_validate_python_dependencies import bench

from test_utils.pre_commit import validate_python_dependencies as validator


@pytest.mark.parametrize(
	"selectors",
	[
		{"platform": "win32"},
		{"python": "^3.14"},
		{"platform": "linux", "python": ">=3.14"},
	],
)
def test_poetry_selectors_are_not_silently_treated_as_unconditional(monkeypatch, selectors):
	declaration = {"version": ">=3", **selectors}
	metadata = {"tool": {"poetry": {"dependencies": {"requests": declaration}}}}
	monkeypatch.setattr(validator, "get_dependencies", lambda app: metadata)
	with pytest.raises(ValueError, match="unsupported Poetry selectors") as error:
		validator.get_versions("custom")
	assert "custom/pyproject.toml" in str(error.value)
	assert "requests" in str(error.value)
	assert all(selector in str(error.value) for selector in selectors)


@pytest.mark.parametrize(
	"metadata,table",
	[
		("project = 42", "project"),
		('project = "invalid"', "project"),
		("project = []", "project"),
		("project = false", "project"),
		('tool = "invalid"', "tool"),
		("tool = []", "tool"),
		("tool = false", "tool"),
		("[tool]\npoetry = 42", "tool.poetry"),
		('''[tool]\npoetry = "invalid"''', "tool.poetry"),
		("[tool]\npoetry = []", "tool.poetry"),
		("[project]\ndependencies = []\n[tool]\npoetry = false", "tool.poetry"),
	],
)
def test_malformed_parent_tables_have_contextual_cli_errors(tmp_path, monkeypatch, capsys, metadata, table):
	bench(tmp_path, monkeypatch, {"custom": "requests"})
	(tmp_path / "apps/custom/pyproject.toml").write_text(metadata)
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert "custom/pyproject.toml" in output.err
	assert f"{table} must be a table" in output.err
	assert "Traceback" not in output.err
	assert "Version mismatch" not in output.out


@pytest.mark.parametrize("selector,value", [("platform", "win32"), ("python", "^3.14")])
def test_selector_cli_errors_explain_how_to_express_the_condition(
	tmp_path, monkeypatch, capsys, selector, value
):
	bench(tmp_path, monkeypatch, {"custom": "requests"})
	(tmp_path / "apps/custom/pyproject.toml").write_text(
		f'[tool.poetry.dependencies]\nrequests = {{version = ">=3", {selector} = "{value}"}}'
	)
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert "requests" in output.err
	assert "markers" in output.err
	assert "Traceback" not in output.err


@pytest.mark.parametrize("marker", ["42", "false", "[]", '"not a valid marker"'])
def test_invalid_poetry_markers_also_include_dependency_context(tmp_path, monkeypatch, capsys, marker):
	bench(tmp_path, monkeypatch, {"custom": "requests"})
	(tmp_path / "apps/custom/pyproject.toml").write_text(
		f'[tool.poetry.dependencies]\nrequests = {{version = ">=3", markers = {marker}}}'
	)
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert "custom/pyproject.toml" in output.err
	assert "requests" in output.err
	assert "markers" in output.err
	assert "Traceback" not in output.err


@pytest.mark.parametrize("applies", [False, True])
def test_supported_markers_retain_environment_filtering(monkeypatch, applies):
	marker = "python_version >= '0'" if applies else "sys_platform == 'not-a-platform'"
	metadata = {"tool": {"poetry": {"dependencies": {"requests": {"version": ">=3", "markers": marker}}}}}
	monkeypatch.setattr(validator, "get_dependencies", lambda app: metadata)
	assert validator.get_versions("custom") == ({"requests": ">=3"} if applies else {})


@pytest.mark.parametrize("app", ["frappe", "custom"])
def test_toml_parse_errors_identify_the_failing_app_in_multi_app_scans(tmp_path, monkeypatch, capsys, app):
	bench(tmp_path, monkeypatch, {"frappe": "requests", "custom": "requests"})
	path = tmp_path / "apps" / app / "pyproject.toml"
	path.write_text("[project]\ndependencies = [invalid]\n")
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert str(path) in output.err
	assert "line 2" in output.err
	assert "Invalid value" in output.err
	assert "Traceback" not in output.err


def test_invalid_utf8_metadata_identifies_its_file_without_a_traceback(tmp_path, monkeypatch, capsys):
	bench(tmp_path, monkeypatch, {"custom": "requests"})
	path = tmp_path / "apps/custom/pyproject.toml"
	path.write_bytes(b"[project]\nname = '\xff'\n")
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert str(path) in output.err
	assert "Traceback" not in output.err


def test_read_errors_include_the_metadata_file_even_without_an_os_filename(tmp_path, monkeypatch, capsys):
	bench(tmp_path, monkeypatch, {"custom": "requests"})
	path = tmp_path / "apps/custom/pyproject.toml"

	def denied(*args, **kwargs):
		raise PermissionError("permission denied")

	monkeypatch.setattr(validator, "open", denied, raising=False)
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert str(path) in output.err
	assert "permission denied" in output.err
	assert "Traceback" not in output.err
