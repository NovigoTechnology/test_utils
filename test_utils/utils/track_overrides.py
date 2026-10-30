"""Shared utilities for tracking method overrides across repositories."""

import difflib
import os

import requests

from test_utils.utils.override_metadata import github_repository
from test_utils.utils.python_source import extract_function


class UpstreamFetchError(Exception):
	"""Upstream source could not be verified, distinct from an override actually changing."""


def download_file_from_commit(repo_url, commit_hash, file_path):
	try:
		username, repo_name = github_repository(repo_url)
	except ValueError as exc:
		raise UpstreamFetchError(str(exc)) from exc
	token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

	if token:
		# raw.githubusercontent.com rate limits per IP and ignores the token, which 429s on
		# shared CI runners. The contents API honours it.
		response = requests.get(
			f"https://api.github.com/repos/{username}/{repo_name}/contents/{file_path}",
			params={"ref": commit_hash},
			headers={
				"Authorization": f"token {token}",
				"Accept": "application/vnd.github.raw",
			},
			timeout=30,
		)
	else:
		response = requests.get(
			f"https://raw.githubusercontent.com/{username}/{repo_name}/{commit_hash}/{file_path}",
			timeout=30,
		)

	if response.status_code == 200:
		return response.text
	raise UpstreamFetchError(
		f"Failed to fetch file {file_path} from commit {commit_hash}: "
		f"{response.status_code} - {response.reason}"
	)


def extract_method(source_code, method_name):
	if not source_code:
		return None
	return extract_function(source_code, method_name)


def compare_method_diff(old_method, new_method):
	if old_method is None and new_method is None:
		return "Tracked method could not be resolved in either version; check PATH and use a qualified METHOD (Class.method)."
	diff = difflib.unified_diff(
		(old_method or "").splitlines(),
		(new_method or "").splitlines(),
		lineterm="",
		fromfile="old",
		tofile="new",
	)
	return "\n".join(diff)
