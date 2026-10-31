"""Read one immutable repository revision without using working-tree files."""

import os
import re
from functools import cached_property
from pathlib import Path, PurePosixPath

import requests

from test_utils.utils.local_overrides import git_result
from test_utils.utils.override_metadata import github_repository
from test_utils.utils.track_overrides import UpstreamFetchError, download_file_from_commit


class RevisionSources:
	def __init__(self, repo_url, revision, *, checkout=None, download=download_file_from_commit):
		if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
			raise UpstreamFetchError("HASH must be a full 40-character hexadecimal commit SHA")
		self.repo_url = repo_url
		self.revision = revision
		self.checkout = Path(checkout) if checkout is not None else None
		self.download = download
		self.files = {}

	@staticmethod
	def relative_path(path):
		if not isinstance(path, str) or "\x00" in path:
			raise UpstreamFetchError("PATH must be a relative repository path without '..'")
		value = PurePosixPath(path)
		if value.is_absolute() or ".." in value.parts:
			raise UpstreamFetchError("PATH must be a relative repository path without '..'")
		return value.as_posix()

	@cached_property
	def local(self):
		return (
			self.checkout is not None
			and git_result(self.checkout, "rev-parse", "--verify", f"{self.revision}^{{commit}}").returncode
			== 0
		)

	@cached_property
	def paths(self):
		if self.local:
			result = git_result(self.checkout, "ls-tree", "-rz", self.revision)
			if result.returncode:
				raise UpstreamFetchError(
					f"Cannot list source files at {self.revision} in {self.checkout}: {result.stderr.strip()}"
				)
			return {
				path
				for entry in result.stdout.split("\x00")
				if entry
				for header, path in [entry.split("\t", 1)]
				if header.split()[1] == "blob"
			}

		try:
			owner, repository = github_repository(self.repo_url)
		except ValueError as exc:
			raise UpstreamFetchError(str(exc)) from exc
		headers = {}
		if token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
			headers["Authorization"] = f"token {token}"
		try:
			response = requests.get(
				f"https://api.github.com/repos/{owner}/{repository}/git/trees/{self.revision}",
				params={"recursive": "1"},
				headers=headers or None,
				timeout=30,
			)
		except requests.RequestException as exc:
			raise UpstreamFetchError(f"Cannot list source files at {self.revision}: {exc}") from exc
		if response.status_code != 200:
			raise UpstreamFetchError(
				f"Cannot list source files at {self.revision}: {response.status_code} - {response.reason}"
			)
		try:
			data = response.json()
			if (
				not isinstance(data, dict)
				or data.get("truncated") is not False
				or not isinstance(data.get("tree"), list)
			):
				raise ValueError("incomplete or invalid Git tree")
			paths = set()
			for entry in data["tree"]:
				if not isinstance(entry, dict) or entry.get("type") not in {"blob", "tree", "commit"}:
					raise ValueError("invalid Git tree entry")
				if entry.get("type") == "blob":
					paths.add(self.relative_path(entry["path"]))
		except (KeyError, TypeError, ValueError) as exc:
			raise UpstreamFetchError(f"Cannot list source files at {self.revision}: {exc}") from exc
		return paths

	def is_file(self, path):
		return self.relative_path(path) in self.paths

	def is_dir(self, path):
		path = self.relative_path(path)
		paths = self.paths
		return path == "." or any(name.startswith(f"{path}/") for name in paths)

	def read(self, path):
		path = self.relative_path(path)
		if path not in self.paths:
			return None
		if path not in self.files:
			if self.local:
				result = git_result(self.checkout, "show", f"{self.revision}:{path}")
				if result.returncode:
					raise UpstreamFetchError(
						f"Cannot read {path} at {self.revision} in {self.checkout}: {result.stderr.strip()}"
					)
				source = result.stdout
			else:
				source = self.download(self.repo_url, self.revision, path)
			if not isinstance(source, str):
				raise UpstreamFetchError(f"Cannot read {path} at {self.revision}")
			self.files[path] = source
		return self.files[path]
