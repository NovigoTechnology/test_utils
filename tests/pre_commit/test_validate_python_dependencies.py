import pytest
from packaging.markers import default_environment

from test_utils.pre_commit import validate_python_dependencies as validator

PLUTOPRINT = (
	"plutoprint~=0.22.0; (sys_platform == 'linux' and platform_machine == 'x86_64') "
	"or (sys_platform == 'darwin' and platform_machine == 'arm64') or sys_platform == 'win32'"
)


def project(dependencies, backend="flit_core.buildapi"):
	return {"build-system": {"build-backend": backend}, "project": {"dependencies": dependencies}}


def test_real_frappe_requirement_with_five_marker_equalities_does_not_crash(monkeypatch):
	monkeypatch.setattr(validator, "get_dependencies", lambda app: project([PLUTOPRINT]))
	# Make the marker independent of the machine running this regression.
	environment = default_environment()
	environment.update(sys_platform="linux", platform_machine="x86_64")
	monkeypatch.setattr("packaging.markers.default_environment", lambda: environment.copy())
	assert validator.get_versions("frappe") == {"plutoprint": "~=0.22.0"}


@pytest.mark.parametrize(
	"backend", ["flit_core.buildapi", "poetry.core.masonry.api", "setuptools.build_meta"]
)
def test_standard_project_dependencies_do_not_depend_on_the_build_backend(monkeypatch, backend):
	monkeypatch.setattr(validator, "get_dependencies", lambda app: project(["requests>=2.0,<3"], backend))
	assert validator.get_versions("app") == {"requests": "<3,>=2.0"}


@pytest.mark.parametrize(
	"requirement,expected",
	[
		("requests==2.0", {"requests": "==2.0"}),
		("requests~=2.0", {"requests": "~=2.0"}),
		("requests!=2.0,>=1.0", {"requests": "!=2.0,>=1.0"}),
		("requests<=2.0", {"requests": "<=2.0"}),
		("Requests[security] >=2.0", {"requests": ">=2.0"}),
		("my_package", {"my-package": ""}),
		("requests @ https://example.invalid/requests.whl", {"requests": ""}),
		('requests>=2.0; sys_platform == "not-a-platform"', {}),
		("requests>=1.0; python_version >= '0'", {"requests": ">=1.0"}),
	],
)
def test_requirement_names_specifiers_extras_and_markers(monkeypatch, requirement, expected):
	monkeypatch.setattr(validator, "get_dependencies", lambda app: project([requirement]))
	assert validator.get_versions("app") == expected


def test_multiple_active_requirements_for_one_package_are_combined(monkeypatch):
	monkeypatch.setattr(validator, "get_dependencies", lambda app: project(["requests>=2.0", "requests<3"]))
	assert validator.get_versions("app") == {"requests": "<3,>=2.0"}


def test_legacy_poetry_versions_are_read_from_values_instead_of_package_names(monkeypatch):
	metadata = {
		"tool": {
			"poetry": {
				"dependencies": {
					"python": "^3.14",
					"requests": "^2.0",
					"my_package": {"version": "1.2.3", "extras": ["extra"]},
				}
			}
		}
	}
	monkeypatch.setattr(validator, "get_dependencies", lambda app: metadata)
	assert validator.get_versions("app") == {"requests": "^2.0", "my-package": "==1.2.3"}


def test_missing_metadata_has_no_dependencies(monkeypatch):
	monkeypatch.setattr(validator, "get_dependencies", lambda app: None)
	assert validator.get_versions("app") == {}


def test_poetry_union_constraint_is_preserved_as_declared(monkeypatch):
	metadata = {"tool": {"poetry": {"dependencies": {"requests": "2.0 || 3.0"}}}}
	monkeypatch.setattr(validator, "get_dependencies", lambda app: metadata)
	assert validator.get_versions("app") == {"requests": "2.0 || 3.0"}


@pytest.mark.parametrize("value", [["requests>=2"], {"requests": ["^2", "^3"]}])
def test_unsupported_poetry_shapes_have_contextual_errors(monkeypatch, value):
	monkeypatch.setattr(
		validator, "get_dependencies", lambda app: {"tool": {"poetry": {"dependencies": value}}}
	)
	with pytest.raises(ValueError, match=r"custom/pyproject\.toml"):
		validator.get_versions("custom")


def bench(tmp_path, monkeypatch, dependencies):
	apps = tmp_path / "apps"
	for name, requirement in dependencies.items():
		path = apps / name
		path.mkdir(parents=True)
		(path / "pyproject.toml").write_text(f'[project]\ndependencies = ["{requirement}"]\n')
	(tmp_path / "sites").mkdir()
	(tmp_path / "sites/apps.txt").write_text("\n".join(dependencies) + "\n\n")
	monkeypatch.chdir(apps / next(iter(dependencies)))


@pytest.mark.parametrize("different", [False, True])
def test_cli_still_reports_real_version_mismatches(tmp_path, monkeypatch, capsys, different):
	bench(
		tmp_path,
		monkeypatch,
		{"frappe": "requests>=2.0", "custom": "requests>=3.0" if different else "Requests>=2.0"},
	)
	assert validator.main(["pyproject.toml"]) == int(different)
	output = capsys.readouterr()
	assert ("Version mismatch for requests" in output.out) is different
	assert "Traceback" not in output.err


def test_invalid_requirement_is_an_actionable_error_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
	bench(tmp_path, monkeypatch, {"frappe": "requests=>2.0"})
	assert validator.main([]) == 1
	output = capsys.readouterr()
	assert "frappe" in output.err
	assert "project.dependencies" in output.err
	assert "Traceback" not in output.err


def test_missing_bench_apps_file_is_reported_without_a_traceback(tmp_path, monkeypatch, capsys):
	monkeypatch.chdir(tmp_path)
	assert validator.main([]) == 1
	assert "apps.txt" in capsys.readouterr().err
