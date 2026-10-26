import argparse
import ast
import datetime
import pathlib
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Sequence


def get_dependencies():
	pyproject = pathlib.Path.cwd() / "pyproject.toml"
	if pyproject.is_file():
		with pyproject.open("rb") as file:
			return tomllib.load(file)
	return {}


def validate_copyright(app=None, files=None):
	# Accept the fork's files-only API as well as upstream's (app, files).
	if files is None and isinstance(app, (list, tuple)):
		files, app = app, None
	if app is None:
		metadata = get_dependencies()
		app = metadata.get("project", {}).get("name") or metadata.get("tool", {}).get("poetry", {}).get(
			"name"
		)
		if not app:
			raise ValueError("Cannot infer the app from pyproject.toml; use --app APP")
		app = app.replace("-", "_")

	app_publisher = ""
	if isinstance(app, (str, pathlib.Path)):
		hooks_file = pathlib.Path(app) / "hooks.py"
		for node in ast.parse(hooks_file.read_text()).body:
			if isinstance(node, ast.Assign) and any(
				isinstance(target, ast.Name) and target.id == "app_publisher" for target in node.targets
			):
				app_publisher = ast.literal_eval(node.value)
				break
	elif isinstance(app, dict):
		app_publisher = app.get("app_publisher", "")
	else:
		raise ValueError("app must be an app path or dictionary with 'app_publisher'")

	year = datetime.datetime.now().year
	for file in files or []:
		suffix = pathlib.Path(file).suffix
		if suffix in (".js", ".ts"):
			prefix, license_prefix, ending = "//", "// ", ""
		elif suffix == ".py":
			prefix, license_prefix, ending = "#", "# ", ""
		elif suffix in (".md", ".html"):
			prefix, license_prefix, ending = "<!--", "", "-->"
		elif suffix == ".sql":
			prefix, license_prefix, ending = "--", "-- ", ""
		else:
			continue
		initial_string = f"{prefix} Copyright (c) "
		copyright_string = (
			f"{initial_string}{year}, {app_publisher} and contributors\n"
			f"{license_prefix}For license information, please see license.txt{ending}\n"
		)
		validate_and_write_file(file, initial_string, copyright_string)


def validate_and_write_file(file, initial_string, copyright_string):
	path = pathlib.Path(file)
	original = path.read_text()
	lines = original.splitlines(keepends=True)
	if lines and lines[0].startswith(initial_string):
		lines.pop(0)
		if lines and "license.txt" in lines[0]:
			lines.pop(0)
	updated = copyright_string + "".join(lines)
	if updated == original:
		return

	# Close the temporary file before replacing the original, including on Windows.
	with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp_file:
		temp_file.write(updated)
		temp_path = pathlib.Path(temp_file.name)
	try:
		temp_path.chmod(path.stat().st_mode)
		shutil.move(temp_path, path)
	finally:
		temp_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None):
	parser = argparse.ArgumentParser()
	parser.add_argument("filenames", nargs="*")
	parser.add_argument("--app", action="append", help="App path (inferred from pyproject.toml if omitted)")
	args = parser.parse_args(argv)
	if args.filenames:
		validate_copyright(args.app[0] if args.app else None, args.filenames)
	sys.exit(0)


if __name__ == "__main__":
	main()
