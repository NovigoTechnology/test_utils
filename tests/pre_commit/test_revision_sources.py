"""Source snapshots must distinguish missing files from inaccessible revisions."""

import subprocess
from types import SimpleNamespace

import pytest

from test_utils.utils import revision_sources
from test_utils.utils.revision_sources import RevisionSources
from test_utils.utils.track_overrides import UpstreamFetchError

REPO = "https://github.com/example/upstream"
SHA = "a" * 40


def git(path, *arguments):
	return subprocess.run(
		["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
	).stdout.strip()


def test_local_snapshots_read_their_own_history_and_ignore_working_tree(tmp_path, monkeypatch):
	git(tmp_path, "init", "-q")
	git(tmp_path, "config", "user.name", "Override Tests")
	git(tmp_path, "config", "user.email", "overrides@example.invalid")
	path = tmp_path / "upstream/api.py"
	path.parent.mkdir()
	path.write_text("def value(): return 'before'\n")
	git(tmp_path, "add", ".")
	git(tmp_path, "commit", "-qm", "before")
	before = git(tmp_path, "rev-parse", "HEAD")
	path.write_text("def value(): return 'after'\n")
	git(tmp_path, "commit", "-qam", "after")
	after = git(tmp_path, "rev-parse", "HEAD")
	path.write_text("def value(): return 'uncommitted'\n")
	monkeypatch.setattr(
		revision_sources.requests, "get", lambda *args, **kwargs: pytest.fail("Unexpected network request")
	)
	old = RevisionSources(REPO, before, checkout=tmp_path)
	new = RevisionSources(REPO, after, checkout=tmp_path)
	assert "'before'" in old.read("upstream/api.py")
	assert "'after'" in new.read("upstream/api.py")
	assert old.is_file("upstream/api.py")
	assert old.is_dir("upstream")
	assert not old.is_file("upstream/missing.py")
	assert old.read("upstream/missing.py") is None
	assert old.read("upstream/api.py") == "def value(): return 'before'\n"


def test_remote_tree_and_sources_are_cached_and_missing_paths_are_not_downloaded(monkeypatch):
	calls = []
	monkeypatch.setenv("GH_TOKEN", "test-token")

	def tree(url, **kwargs):
		calls.append((url, kwargs))
		return SimpleNamespace(
			status_code=200,
			json=lambda: {
				"truncated": False,
				"tree": [{"type": "blob", "path": "upstream/api.py"}],
			},
		)

	monkeypatch.setattr(revision_sources.requests, "get", tree)
	downloads = []

	def download(repo, revision, path):
		downloads.append((repo, revision, path))
		return "def value(): pass\n"

	source = RevisionSources(REPO, SHA, download=download)
	assert source.is_dir("upstream")
	assert source.is_file("upstream/api.py")
	assert source.read("upstream/api.py") == source.read("upstream/api.py")
	assert source.read("upstream/missing.py") is None
	assert downloads == [(REPO, SHA, "upstream/api.py")]
	assert len(calls) == 1
	assert calls[0][1]["headers"]["Authorization"] == "token test-token"


def test_existing_remote_file_download_failure_is_not_a_confirmed_deletion(monkeypatch):
	monkeypatch.setattr(
		revision_sources.requests,
		"get",
		lambda *args, **kwargs: SimpleNamespace(
			status_code=200,
			json=lambda: {"truncated": False, "tree": [{"type": "blob", "path": "upstream/api.py"}]},
		),
	)

	def unavailable(*args):
		raise UpstreamFetchError("404 - cannot fetch source")

	source = RevisionSources(REPO, SHA, download=unavailable)
	with pytest.raises(UpstreamFetchError, match="404"):
		source.read("upstream/api.py")


def test_remote_tree_must_be_available_complete_and_valid(monkeypatch):
	responses = [
		SimpleNamespace(status_code=404, reason="Not Found"),
		SimpleNamespace(status_code=200, json=lambda: {"truncated": True, "tree": []}),
		SimpleNamespace(status_code=200, json=lambda: ["unexpected JSON"]),
	]
	for response in responses:
		monkeypatch.setattr(revision_sources.requests, "get", lambda *args, **kwargs: response)
		with pytest.raises(UpstreamFetchError, match="Cannot list source files"):
			RevisionSources(REPO, SHA).read("upstream/api.py")


def test_invalid_revision_and_unsafe_paths_are_rejected_before_access():
	with pytest.raises(UpstreamFetchError, match="40-character"):
		RevisionSources(REPO, "HEAD")
	source = RevisionSources(REPO, SHA)
	for path in ["/upstream/api.py", "../api.py", "upstream/../api.py", "api\x00.py"]:
		with pytest.raises(UpstreamFetchError, match="relative"):
			source.read(path)
