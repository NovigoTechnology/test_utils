"""Compare annotated upstream source against matching local Git checkouts."""

import re
import subprocess
from pathlib import Path, PurePosixPath

from test_utils.utils.override_metadata import repository_identity
from test_utils.utils.track_overrides import UpstreamFetchError, download_file_from_commit


def git_result(directory, *arguments):
	try:
		return subprocess.run(
			["git", "-C", str(directory), *arguments],
			capture_output=True,
			encoding="utf-8",
			errors="replace",
			check=False,
		)
	except OSError as exc:
		raise UpstreamFetchError(f"Cannot read Git checkout {directory}: {exc}") from exc


def repository_root(directory):
	result = git_result(directory, "rev-parse", "--show-toplevel")
	return Path(result.stdout.strip()) if result.returncode == 0 else None


class LocalUpstreamSources:
	def __init__(self, directory, apps_root=None):
		self.directory = Path(directory).resolve()
		self.apps_root = Path(apps_root).resolve() if apps_root else None
		self.repositories = None
		self.heads = {}
		self.files = {}

	def discover(self):
		root = repository_root(self.directory) or self.directory
		contexts = (
			[self.apps_root]
			if self.apps_root
			else {
				self.directory,
				self.directory.parent,
				self.directory / "apps",
				root,
				root.parent,
				root / "apps",
			}
		)
		candidates = set()
		for context in contexts:
			if not context.is_dir():
				continue
			for path in [context, *context.iterdir()]:
				if path.is_dir() and (path / ".git").exists():
					candidates.add(path.resolve())
		self.repositories = {}
		for path in sorted(candidates):
			remotes = git_result(path, "config", "--get-regexp", r"^remote\..*\.url$")
			for line in remotes.stdout.splitlines():
				_, separator, url = line.partition(" ")
				if not separator:
					continue
				try:
					identity = repository_identity(url.strip())
				except ValueError:
					continue
				self.repositories.setdefault(identity, set()).add(path)

	def checkout(self, repo_url):
		try:
			identity = repository_identity(repo_url)
		except ValueError as exc:
			raise UpstreamFetchError(str(exc)) from exc
		if self.repositories is None:
			self.discover()
		matches = self.repositories.get(identity, set())
		if len(matches) != 1:
			problem = "No matching local checkout" if not matches else "Multiple matching local checkouts"
			raise UpstreamFetchError(
				f"{problem} for {identity}; use --apps-root to select dependency checkouts "
				"or --base-branch for a remote comparison"
			)
		return next(iter(matches))

	def head(self, checkout):
		if checkout not in self.heads:
			result = git_result(checkout, "rev-parse", "--verify", "HEAD^{commit}")
			if result.returncode:
				raise UpstreamFetchError(f"Cannot resolve local HEAD in {checkout}")
			self.heads[checkout] = result.stdout.strip()
		return self.heads[checkout]

	def source(self, checkout, revision, path):
		key = (checkout, revision, path)
		if key not in self.files:
			result = git_result(checkout, "show", f"{revision}:{path}")
			self.files[key] = result.stdout if result.returncode == 0 else None
		return self.files[key]

	def compare_sources(self, repo_url, baseline, file_path):
		if not re.fullmatch(r"[0-9a-fA-F]{40}", baseline):
			raise UpstreamFetchError("HASH must be a full 40-character hexadecimal commit SHA")
		path = PurePosixPath(file_path)
		if not file_path or path.is_absolute() or ".." in path.parts or "\x00" in file_path:
			raise UpstreamFetchError("PATH must be a relative upstream repository path without '..'")
		checkout = self.checkout(repo_url)
		head = self.head(checkout)
		old = self.source(checkout, baseline, file_path)
		if old is None:
			# Shallow checkouts may not contain the annotated historical commit.
			key = (checkout, baseline, file_path)
			old = download_file_from_commit(repo_url, baseline, file_path)
			self.files[key] = old
		new = self.source(checkout, head, file_path)
		if new is None:
			# Verify deletion rather than treating arbitrary Git errors as drift.
			result = git_result(
				checkout, "--literal-pathspecs", "ls-tree", "--name-only", head, "--", file_path
			)
			if result.returncode or result.stdout.strip():
				raise UpstreamFetchError(f"Cannot read {file_path} at local HEAD {head} in {checkout}")
		return old, new
