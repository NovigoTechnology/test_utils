"""Exercise argument-free tracking with real, isolated dependency repositories."""

import subprocess

import pytest
import requests

from test_utils.pre_commit import track_overrides as tracker
from test_utils.utils import local_overrides
from test_utils.utils.track_overrides import UpstreamFetchError


def git(path, *arguments):
	return subprocess.run(
		["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
	).stdout.strip()


def repository(path, repo, remote="origin"):
	path.mkdir(parents=True)
	git(path, "init", "-q")
	git(path, "config", "user.name", "Override Tests")
	git(path, "config", "user.email", "overrides@example.invalid")
	git(path, "remote", "add", remote, repo)
	return path


def commit(path):
	git(path, "add", ".")
	git(path, "commit", "-qm", "test: update upstream source")
	return git(path, "rev-parse", "HEAD")


def annotated(path, sha, app="custom", repo="https://github.com/example/upstream"):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		f'def custom():\n    """\n    APP: {app}\n    HASH: {sha}\n'
		f'    REPO: {repo}\n    PATH: upstream/api.py\n    METHOD: value\n    """\n    pass\n'
	)


@pytest.fixture
def bench(tmp_path, monkeypatch):
	apps = tmp_path / "apps"
	custom = repository(apps / "custom", "https://github.com/example/custom")
	upstream = repository(apps / "dependency", "git@github.com:example/upstream.git", remote="upstream")
	(upstream / "upstream").mkdir()
	(upstream / "upstream/api.py").write_text("def value():\n    return 'before'\n")
	sha = commit(upstream)
	annotated(custom / "custom/override.py", sha)
	commit(custom)
	monkeypatch.chdir(custom)
	monkeypatch.setattr(
		tracker.requests, "get", lambda *args, **kwargs: pytest.fail("Unexpected network request")
	)
	return apps, custom, upstream, sha


def run(argv=None):
	with pytest.raises(SystemExit) as result:
		tracker.main(argv or [])
	return result.value.code


def test_no_arguments_checks_the_current_repository_against_local_head(bench, capsys):
	assert run() == 0
	assert "local Git HEAD" in capsys.readouterr().out


def test_no_arguments_detects_method_changes_on_a_detached_dependency_head(bench, capsys):
	_, _, upstream, _ = bench
	(upstream / "upstream/api.py").write_text("def value():\n    return 'after'\n")
	sha = commit(upstream)
	git(upstream, "checkout", "--detach", sha)
	assert run() == 1
	output = capsys.readouterr().out
	assert "return 'after'" in output
	assert "return 'before'" in output


def test_unrelated_file_changes_and_uncommitted_dependency_edits_are_not_committed_drift(bench):
	_, _, upstream, _ = bench
	(upstream / "notes.txt").write_text("An unrelated change")
	commit(upstream)
	(upstream / "upstream/api.py").write_text("def value(): return 'uncommitted'")
	assert run() == 0


@pytest.mark.parametrize("deletion", ["method", "file"])
def test_removed_upstream_methods_or_files_fail(bench, deletion):
	_, _, upstream, _ = bench
	path = upstream / "upstream/api.py"
	if deletion == "file":
		path.unlink()
	else:
		path.write_text("def renamed(): pass")
	commit(upstream)
	assert run() == 1


def test_nested_invocation_scans_the_app_repository_instead_of_only_the_subdirectory(bench, monkeypatch):
	_, custom, upstream, _ = bench
	(upstream / "upstream/api.py").write_text("def value(): return 'changed'")
	commit(upstream)
	(custom / "nested").mkdir()
	monkeypatch.chdir(custom / "nested")
	assert run() == 1


def test_invalid_hash_fails_as_unverified_instead_of_succeeding(bench, monkeypatch, capsys):
	_, custom, _, _ = bench
	annotated(custom / "custom/override.py", "a" * 40)

	def unavailable(*args):
		raise UpstreamFetchError("404 - annotated commit not found")

	monkeypatch.setattr(local_overrides, "download_file_from_commit", unavailable)
	assert run() == 2
	assert "404" in capsys.readouterr().out


def test_shallow_history_can_read_the_baseline_remotely(bench, monkeypatch):
	_, custom, _, _ = bench
	annotated(custom / "custom/override.py", "a" * 40)
	calls = []

	def baseline(repo, sha, path):
		calls.append((repo, sha, path))
		return "def value():\n    return 'before'\n"

	monkeypatch.setattr(local_overrides, "download_file_from_commit", baseline)
	assert run() == 0
	assert len(calls) == 1


def test_network_errors_are_unverified_without_a_traceback(bench, monkeypatch, capsys):
	_, custom, _, _ = bench
	annotated(custom / "custom/override.py", "a" * 40)

	def unavailable(*args):
		raise requests.ConnectionError("Network unavailable")

	monkeypatch.setattr(local_overrides, "download_file_from_commit", unavailable)
	assert run() == 2
	output = capsys.readouterr()
	assert "Network unavailable" in output.out
	assert "Traceback" not in output.err


def test_missing_checkout_does_not_guess_the_current_app_branch(bench, capsys):
	_, custom, _, sha = bench
	annotated(custom / "custom/override.py", sha, repo="https://github.com/example/missing")
	assert run() == 2
	assert "No matching local checkout" in capsys.readouterr().out


def test_duplicate_checkouts_require_an_explicit_dependency_context(bench, capsys):
	apps, _, upstream, _ = bench
	other = repository(apps / "another-copy", "https://github.com/example/upstream")
	(other / "data.txt").write_text("another checkout")
	commit(other)
	assert run() == 2
	assert "Multiple matching local checkouts" in capsys.readouterr().out
	assert run(["--apps-root", str(upstream)]) == 0


@pytest.mark.parametrize(
	"arguments", [["--app", "custom"], ["--directory", "."], ["--base-branch", "target-branch"]]
)
def test_existing_optional_arguments_work_with_defaults_for_the_remaining_options(
	bench, monkeypatch, arguments
):
	_, _, _, sha = bench
	if "--base-branch" in arguments:
		monkeypatch.setattr(
			tracker, "get_last_commit_hash_for_file_in_branch", lambda repo, path, branch: sha
		)
	assert run(arguments) == 0


def test_explicit_directory_with_app_filter_excludes_unresolvable_annotations(bench):
	apps, _, _, sha = bench
	annotated(apps / "another/override.py", sha, app="another", repo="https://github.com/example/missing")
	assert run(["--directory", str(apps), "--app", "custom"]) == 0


def test_nonexistent_directory_is_a_configuration_error(bench):
	assert run(["--directory", "does-not-exist"]) == 2


def test_no_annotations_still_runs_and_reports_the_scan(bench, capsys):
	_, custom, _, _ = bench
	(custom / "custom/override.py").write_text("def helper(): pass")
	assert run() == 0
	assert "scanning" in capsys.readouterr().out
