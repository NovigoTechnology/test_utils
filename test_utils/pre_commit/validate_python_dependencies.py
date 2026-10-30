import argparse
import pathlib
import sys
import tomllib
from collections.abc import Sequence

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name


def get_dependencies(app):
	apps_dir = pathlib.Path().resolve().parent
	if pathlib.Path.exists(apps_dir / app / "pyproject.toml"):
		with open(apps_dir / app / "pyproject.toml", "rb") as f:
			return tomllib.load(f)


def poetry_versions(app, dependencies):
	if not isinstance(dependencies, dict):
		raise ValueError(f"{app}/pyproject.toml: tool.poetry.dependencies must be a table")
	versions = {}
	for package, declaration in dependencies.items():
		if package == "python":
			continue
		if isinstance(declaration, dict):
			selectors = sorted({"python", "platform"}.intersection(declaration))
			if selectors:
				raise ValueError(
					f"{app}/pyproject.toml: unsupported Poetry selectors {', '.join(selectors)} for {package}; "
					"express the condition with markers or standard project.dependencies requirements"
				)
			if "markers" in declaration:
				marker = declaration["markers"]
				try:
					if not isinstance(marker, str):
						raise ValueError("markers must be a string")
					applies = Marker(marker).evaluate()
				except ValueError:
					raise ValueError(
						f"{app}/pyproject.toml: invalid Poetry markers for {package}; expected a PEP 508 marker string"
					) from None
				if not applies:
					continue
			declaration = declaration.get("version", "*")
		if not isinstance(declaration, str):
			raise ValueError(f"{app}/pyproject.toml: unsupported Poetry dependency declaration for {package}")
		constraint = declaration.strip()
		if constraint == "*":
			constraint = ""
		elif constraint[:1].isdigit():
			constraint = "==" + constraint
		try:
			constraint = str(SpecifierSet(constraint))
		except InvalidSpecifier:
			# Poetry caret, tilde and union syntax is compared as declared.
			constraint = declaration.strip()
		versions[canonicalize_name(package)] = constraint
	return versions


def metadata_table(document, name, app, location):
	value = document.get(name, {})
	if not isinstance(value, dict):
		raise ValueError(f"{app}/pyproject.toml: {location} must be a table")
	return value


def get_versions(app):
	pyproject_toml = get_dependencies(app)
	if pyproject_toml is None:
		return {}
	if not isinstance(pyproject_toml, dict):
		raise ValueError(f"{app}/pyproject.toml: project metadata must be a table")

	project = metadata_table(pyproject_toml, "project", app, "project")
	tool = metadata_table(pyproject_toml, "tool", app, "tool")
	poetry = metadata_table(tool, "poetry", app, "tool.poetry")
	if "dependencies" not in project:
		return poetry_versions(app, poetry.get("dependencies", {}))
	dependencies = project["dependencies"]
	if not isinstance(dependencies, list):
		raise ValueError(f"{app}/pyproject.toml: project.dependencies must be a list of requirement strings")
	dependency_objects = {}
	for index, dep in enumerate(dependencies, start=1):
		try:
			requirement = Requirement(dep)
		except InvalidRequirement, TypeError:
			raise ValueError(
				f"{app}/pyproject.toml: invalid project.dependencies entry #{index}; expected a PEP 508 requirement"
			) from None
		if requirement.marker is not None and not requirement.marker.evaluate():
			continue
		package = canonicalize_name(requirement.name)
		constraints = [dependency_objects.get(package, ""), str(requirement.specifier)]
		dependency_objects[package] = str(SpecifierSet(",".join(filter(None, constraints))))

	return dependency_objects


def get_mismatched_versions():
	apps_order = pathlib.Path().resolve().parent.parent / "sites" / "apps.txt"
	apps_order = [app.strip() for app in apps_order.read_text().splitlines() if app.strip()]
	exceptions = []
	app_packages = {app: get_versions(app) for app in apps_order}
	for app, packages in app_packages.items():
		for package, package_version in packages.items():
			for app2, app2_packages in app_packages.items():
				if app == app2:
					continue

				if package in app2_packages and app2_packages[package] != package_version:
					# Check if exception already exists
					existing_exception = next(
						(exception for exception in exceptions if package in exception), None
					)
					if existing_exception:
						existing_exception[package][app] = package_version
					else:
						exceptions.append({package: {app: package_version}})
	return exceptions


def main(argv: Sequence[str] | None = None):
	parser = argparse.ArgumentParser()
	parser.add_argument("filenames", nargs="*")
	_args = parser.parse_args(argv)

	try:
		exceptions = get_mismatched_versions()
	except (OSError, ValueError) as exc:
		print(f"validate_python_dependencies: {exc}", file=sys.stderr)
		return 1
	if exceptions:
		for exception in exceptions:
			for package, apps in exception.items():
				print(f"\nVersion mismatch for {package} in:")
				for app, version in apps.items():
					print(f"{app}: {version}")

	return 1 if exceptions else 0


if __name__ == "__main__":
	sys.exit(main())
