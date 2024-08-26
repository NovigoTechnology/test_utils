import argparse
import datetime
import os
import pathlib
import sys
import tempfile
from typing import Sequence

import tomli


def get_dependencies():
	apps_dir = pathlib.Path().resolve()
	if pathlib.Path.exists(apps_dir / "pyproject.toml"):
		with open(apps_dir / "pyproject.toml", "rb") as f:
			return tomli.load(f)


def validate_copyright(files):

	year = datetime.datetime.now().year
	app_publisher = ""

	apps_dir = pathlib.Path().resolve()

	hooks_file = f"{apps_dir}/{get_dependencies().get('project').get('name')}/hooks.py"
	with open(hooks_file) as file:
		for line in file:
			if "app_publisher" in line:
				app_publisher = line.split("=")[1].strip().replace('"', "")

	initial_js_string = "// Copyright (c) "
	initial_py_string = "# Copyright (c) "
	initial_md_string = "<!-- Copyright (c) "

	copyright_js_string = f"// Copyright (c) {year}, {app_publisher} and contributors\n// For license information, please see license.txt\n"
	copyright_py_string = f"# Copyright (c) {year}, {app_publisher} and contributors\n# For license information, please see license.txt\n"
	copyright_md_string = f"<!-- Copyright (c) {year}, {app_publisher} and contributors\nFor license information, please see license.txt-->\n"

	for file in files:
		if file.endswith(".js") or file.endswith(".ts"):
			validate_and_write_file(file, initial_js_string, copyright_js_string)

		elif file.endswith(".py"):
			validate_and_write_file(file, initial_py_string, copyright_py_string)

		elif file.endswith(".md") or file.endswith(".html"):
			validate_and_write_file(file, initial_md_string, copyright_md_string)


def validate_and_write_file(file, initial_string, copyright_string):
	# using tempfile to avoid issues while reading large files
	temp_file_path = tempfile.mktemp()
	with open(file) as original_file, open(temp_file_path, "w") as temp_file:
		first_line = original_file.readline()
		if not first_line.startswith(initial_string):
			temp_file.write(copyright_string)
			temp_file.write(first_line)
			temp_file.writelines(original_file)

			# Replace the original file with the temp file
			os.replace(temp_file_path, file)
		else:
			# license.txt
			dos_line = original_file.readline()
			if not "license.txt" in dos_line:
				temp_file.write(copyright_string)
				temp_file.write(dos_line)
			else:
				temp_file.write(copyright_string)

			temp_file.writelines(original_file)
			# Replace the original file with the temp file
			os.replace(temp_file_path, file)


def main(argv: Sequence[str] = None):
	parser = argparse.ArgumentParser()
	parser.add_argument("filenames", nargs="*")
	args = parser.parse_args(argv)

	files = args.filenames
	if files:
		validate_copyright(files)

	sys.exit(0)


if __name__ == "__main__":
	main()
