import argparse
import os
import pathlib
import sys
from collections.abc import Sequence

import requests

from test_utils.utils.local_overrides import LocalUpstreamSources, repository_root
from test_utils.utils.override_metadata import github_repository, iter_annotations
from test_utils.utils.track_overrides import (
	UpstreamFetchError,
	compare_method_diff,
	download_file_from_commit,
	extract_method,
)


def get_all_python_files(repo_path):
	"""Yield (file_path, content) for all .py files in repo_path. file_path is relative to repo_path."""
	for root, directories, files in os.walk(repo_path):
		directories[:] = [
			name
			for name in directories
			if name not in {".git", ".venv", "env", "venv", "node_modules", "__pycache__"}
		]
		for file_name in files:
			if not file_name.endswith(".py"):
				continue
			abs_path = os.path.join(root, file_name)
			relative_path = os.path.relpath(abs_path, repo_path)
			try:
				with open(abs_path, encoding="utf-8", errors="replace") as f:
					content = f.read()
				yield relative_path, content
			except OSError as e:
				print(f"Failed to read {relative_path}: {e}")


def get_last_commit_hash_for_file_in_branch(repo_url, file_path, base_branch):
	try:
		username, repo_name = github_repository(repo_url)
	except ValueError as exc:
		raise UpstreamFetchError(str(exc)) from exc
	api_url = f"https://api.github.com/repos/{username}/{repo_name}/commits"
	params = {"path": file_path, "sha": base_branch}
	headers = {}
	if token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
		headers["Authorization"] = f"token {token}"
	response = requests.get(api_url, params=params, headers=headers or None, timeout=30)

	if response.status_code != 200:
		raise UpstreamFetchError(
			f"Failed to fetch commits for {file_path}: {response.status_code} - {response.reason}"
		)

	commits = response.json()
	return commits[0]["sha"] if commits else None


def print_diff(diff_text, color=True):
	diff_lines = diff_text.splitlines()
	for line in diff_lines:
		if not color:
			print(line)
		elif line.startswith("+++") or line.startswith("---"):
			print("\033[32m" + line)
		elif line.startswith("@@"):
			print("\033[36m" + line)
		elif line.startswith("+"):
			print("\033[32m" + line)
		elif line.startswith("-"):
			print("\033[31m" + line)
		else:
			print("\033[37m" + line)


def check_tracked_methods(repo_directory, base_branch=None, apps_filter=None, *, apps_root=None):
	changed_methods = []
	unverified = []
	local = LocalUpstreamSources(repo_directory, apps_root) if base_branch is None else None
	for file_path, source_code in get_all_python_files(repo_directory):
		for annotation in iter_annotations(source_code):
			app_name = annotation.get("APP")
			commit_hash = annotation["HASH"]
			repo_url = annotation["REPO"]
			original_file_path = annotation["PATH"]
			method_name = annotation["METHOD"]
			# Infer app from file path if APP not in annotation (backward compat)
			if not app_name and "/" in file_path:
				parts = file_path.replace("\\", "/").split("/")
				app_name = parts[1] if parts[0] == "apps" and len(parts) > 1 else parts[0]
			elif not app_name:
				app_name = os.path.basename(repo_directory) if repo_directory else ""
			if apps_filter is not None and app_name not in apps_filter:
				continue

			try:
				if local is not None:
					old_file_content, new_file_content = local.compare_sources(
						repo_url, commit_hash, original_file_path
					)
				else:
					latest_commit_hash = get_last_commit_hash_for_file_in_branch(
						repo_url, original_file_path, base_branch
					)
					if not latest_commit_hash:
						raise UpstreamFetchError(
							f"Cannot resolve upstream revision for {original_file_path} on {base_branch}"
						)
					if latest_commit_hash == commit_hash:
						continue
					old_file_content = download_file_from_commit(repo_url, commit_hash, original_file_path)
					new_file_content = download_file_from_commit(
						repo_url, latest_commit_hash, original_file_path
					)
			except (UpstreamFetchError, requests.RequestException, OSError) as e:
				unverified.append(f"{file_path}: {method_name}: {e}")
				continue

			method_at_hash = extract_method(old_file_content, method_name)
			latest_method = extract_method(new_file_content, method_name)

			diff = compare_method_diff(method_at_hash, latest_method)
			if diff:
				changed_methods.append(
					{
						"title": f"{app_name}: `{method_name}` in `{original_file_path}` has changed:",
						"diff": diff,
					}
				)

	return changed_methods, unverified


def main(argv: Sequence[str] | None = None):
	parser = argparse.ArgumentParser()
	parser.add_argument("--app", action="append", help="App name(s) to scan (resolves to app directory)")
	parser.add_argument(
		"--directory",
		help="Directory to scan (e.g. workspace root); overrides --app when set. Use for monorepos to match GHA behavior.",
	)
	parser.add_argument(
		"--base-branch",
		action="append",
		help="Compare against this remote branch/ref instead of local upstream HEAD",
	)
	parser.add_argument(
		"--apps-root",
		type=pathlib.Path,
		help="Directory containing local dependency checkouts (default: discover sibling apps)",
	)
	parser.add_argument(
		"--no-color",
		action="store_true",
	)
	args = parser.parse_args(argv)

	base_branch = args.base_branch[0] if args.base_branch else None

	if args.directory:
		# Scan entire directory (like GHA with github.workspace)
		directories = [str(pathlib.Path(args.directory).resolve())]
		apps_filter = set(args.app) if args.app else None
	elif args.app:
		# Scan app directory(ies); supports multiple apps for monorepos
		directories = []
		for app in args.app:
			path = pathlib.Path(app)
			if not path.is_dir():
				path = pathlib.Path.cwd().parent / app
			if not path.is_dir():
				parser.error(f"App directory not found: {app}")
			root = repository_root(path)
			if root is not None and path.resolve().parent == root.resolve():
				path = root
			elif (path / "hooks.py").is_file():
				path = path.parent
			directories.append(str(path.resolve()))
		apps_filter = None  # Already scoped by directory
	else:
		try:
			path = repository_root(pathlib.Path.cwd()) or pathlib.Path.cwd()
		except UpstreamFetchError as exc:
			print(str(exc), file=sys.stderr)
			sys.exit(2)
		directories = [str(path)]
		apps_filter = None

	for directory in directories:
		if not pathlib.Path(directory).is_dir():
			parser.error(f"Directory not found: {directory}")
	if args.apps_root is not None and not args.apps_root.is_dir():
		parser.error(f"Dependency directory not found: {args.apps_root}")
	if directories:
		changed_methods = []
		unverified = []
		for repo_directory in directories:
			print(
				f"track_overrides: scanning {repo_directory}; comparing upstream against {base_branch or 'local Git HEAD'}"
			)
			changed, could_not_check = check_tracked_methods(
				repo_directory, base_branch, apps_filter=apps_filter, apps_root=args.apps_root
			)
			changed_methods.extend(changed)
			unverified.extend(could_not_check)

		if changed_methods:
			print("\n[PRE-COMMIT HOOK] Method changes detected! Please review before committing.")
			for change in changed_methods:
				print(change["title"])
				print_diff(change["diff"], color=not args.no_color)
				print("")

		if unverified:
			print("\n[PRE-COMMIT HOOK] Could not check these overrides against upstream:")
			for message in unverified:
				print(f"  {message}")
			print("")

		# Exit 2 says nothing about drift, only that upstream could not be verified. Callers
		# must not read it as a clean run.
		if changed_methods:
			sys.exit(1)
		if unverified:
			sys.exit(2)
	sys.exit(0)


if __name__ == "__main__":
	main()
