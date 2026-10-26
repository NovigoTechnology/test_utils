import datetime

import pytest

from test_utils.pre_commit import validate_copyright as copyright_hook


@pytest.fixture
def app(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	app_dir = tmp_path / "sample_app"
	app_dir.mkdir()
	(app_dir / "hooks.py").write_text("app_publisher = 'Novigo Technology'  # publisher\n")
	return tmp_path


@pytest.mark.parametrize(
	"metadata",
	['[project]\nname = "sample-app"\n', '[tool.poetry]\nname = "sample_app"\n'],
)
def test_infers_app_for_existing_files_only_api(app, metadata):
	(app / "pyproject.toml").write_text(metadata)
	file = app / "example.py"
	file.write_text("print('preserved')\n")
	copyright_hook.validate_copyright([str(file)])
	assert "Novigo Technology and contributors" in file.read_text()
	assert file.read_text().endswith("print('preserved')\n")


@pytest.mark.parametrize(
	"extension,prefix",
	[("js", "//"), ("ts", "//"), ("py", "#"), ("md", "<!--"), ("html", "<!--"), ("sql", "--")],
)
def test_upstream_api_preserves_content_and_is_idempotent(tmp_path, extension, prefix):
	file = tmp_path / f"example.{extension}"
	body = "original content\nsecond line\n"
	file.write_text(body)
	file.chmod(0o755)
	copyright_hook.validate_copyright({"app_publisher": "Publisher"}, [str(file)])
	first_result = file.read_text()
	assert first_result.startswith(f"{prefix} Copyright (c) {datetime.datetime.now().year}, Publisher")
	assert first_result.endswith(body)
	assert file.stat().st_mode & 0o777 == 0o755
	first_stat = file.stat()
	copyright_hook.validate_copyright({"app_publisher": "Publisher"}, [str(file)])
	assert file.read_text() == first_result
	assert file.stat().st_mtime_ns == first_stat.st_mtime_ns
	assert not list(tmp_path.glob("tmp*"))


@pytest.mark.parametrize("license_line", ["# For license information, please see license.txt\n", ""])
def test_refreshes_existing_header_without_losing_code(tmp_path, license_line):
	file = tmp_path / "example.py"
	body = "print('keep first line')\nprint('keep second line')\n"
	file.write_text("# Copyright (c) 2000, Old Publisher and contributors\n" + license_line + body)
	copyright_hook.validate_copyright({"app_publisher": "New Publisher"}, [str(file)])
	result = file.read_text()
	assert result.endswith(body)
	assert result.count("Copyright (c)") == 1
	assert "Old Publisher" not in result
	assert "New Publisher" in result


@pytest.mark.parametrize("explicit_app", [False, True])
def test_cli_supports_inferred_and_explicit_app(app, explicit_app):
	(app / "pyproject.toml").write_text('[project]\nname = "sample_app"\n')
	file = app / "page.html"
	file.write_text("<p>Preserve page</p>\n")
	args = [str(file)]
	if explicit_app:
		args.extend(["--app", "sample_app"])
	with pytest.raises(SystemExit) as result:
		copyright_hook.main(args)
	assert result.value.code == 0
	assert "Novigo Technology" in file.read_text()
	assert file.read_text().endswith("<p>Preserve page</p>\n")


def test_missing_metadata_suggests_explicit_app(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	with pytest.raises(ValueError, match="use --app APP"):
		copyright_hook.validate_copyright(["example.py"])
