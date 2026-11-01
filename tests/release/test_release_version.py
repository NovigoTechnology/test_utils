"""Exercise release calculations with the real pinned CLI in disposable Git repos."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/release_version.py"


def run(root, *args, check=True, env=None):
	return subprocess.run(args, cwd=root, text=True, capture_output=True, check=check, env=env)


def git(root, *args):
	return run(root, "git", *args).stdout.strip()


def published(root, *tags):
	path = root / "published.json"
	path.write_text(
		json.dumps(
			[
				[
					{
						"tag_name": tag,
						"published_at": "2026-11-01T00:00:00Z",
						"draft": False,
						"prerelease": False,
					}
					for tag in tags
				]
			]
		)
	)
	return path


def change(root, message):
	with (root / "changes.txt").open("a") as stream:
		stream.write(message + "\n")
	git(root, "add", "changes.txt")
	git(root, "commit", "-m", message)


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
	# The release workflow installs this tool; unrelated utility suites need not.
	monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
	if not shutil.which("semantic-release"):
		pytest.skip("Install python-semantic-release==10.7.0 to run release integration tests")
	assert "10.7.0" in run(tmp_path, "semantic-release", "--version").stdout
	git(tmp_path, "init", "-q", "--initial-branch=nvg-develop")
	git(tmp_path, "config", "user.name", "Release Test")
	git(tmp_path, "config", "user.email", "release@example.com")
	git(tmp_path, "remote", "add", "origin", "https://github.com/example/release-fixture.git")
	(tmp_path / "pyproject.toml").write_text("""[project]
name = "release-fixture"
version = "1.2.5"

[tool.semantic_release]
version_toml = ["pyproject.toml:project.version"]
commit_parser = "conventional"

[tool.semantic_release.branches.stable]
match = "^v2$"
prerelease = false
""")
	git(tmp_path, "add", "pyproject.toml")
	git(tmp_path, "commit", "-m", "chore: import upstream history")
	git(tmp_path, "tag", "v1.30.1")
	change(tmp_path, "chore: establish published fork history")
	git(tmp_path, "tag", "v1.2.5")
	git(tmp_path, "branch", "v2")
	published(tmp_path, "v1.2.5")
	return tmp_path


def preview(root, *, source=None, check=True, env=None):
	args = [sys.executable, str(SCRIPT), "--published-releases", str(root / "published.json")]
	if source:
		args.extend(["--source-sha", source])
	result = run(root, *args, check=check, env=env)
	values = dict(line.split("=", 1) for line in result.stdout.splitlines())
	return result, values


def promote(root):
	source = git(root, "rev-parse", "nvg-develop")
	git(root, "checkout", "v2")
	git(root, "merge", "--no-ff", "nvg-develop", "-m", "chore: promote development")
	return source


def local_release(root, *, bootstrap=False):
	args = ["semantic-release", "version", "--no-push", "--no-vcs-release", "--skip-build"]
	if bootstrap:
		args.append("--major")
	run(root, *args)


def test_release_cycle_ignores_legacy_tags_and_does_not_bump_after_merge_back(release_repo):
	root = release_repo
	change(root, "feat: add a useful capability")
	source = promote(root)
	head = git(root, "rev-parse", "HEAD")
	output = root / "action-output.txt"
	_, values = preview(root, source=source, env={**os.environ, "GITHUB_OUTPUT": str(output)})
	assert values == {
		"baseline": "1.2.5",
		"version": "2.0.0",
		"tag": "v2.0.0",
		"changed": "true",
		"bootstrap": "true",
	}
	assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == values
	assert git(root, "tag", "--list").splitlines() == ["v1.2.5", "v1.30.1"]
	assert git(root, "rev-parse", "HEAD") == head
	assert preview(root)[1] == values
	local_release(root, bootstrap=True)
	assert git(root, "rev-parse", "v2.0.0^{}") == git(root, "rev-parse", "HEAD")
	assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == "2.0.0"
	assert (root / "CHANGELOG.md").is_file()
	published(root, "v1.2.5", "v2.0.0")
	git(root, "checkout", "nvg-develop")
	git(root, "merge", "--no-ff", "v2", "-m", "chore: synchronize published release")
	source = promote(root)
	_, unchanged = preview(root, source=source)
	assert unchanged == {
		"baseline": "2.0.0",
		"version": "2.0.0",
		"tag": "v2.0.0",
		"changed": "false",
		"bootstrap": "false",
	}
	head = git(root, "rev-parse", "HEAD")
	local_release(root)
	assert git(root, "rev-parse", "HEAD") == head
	assert preview(root)[1] == unchanged
	git(root, "checkout", "nvg-develop")
	change(root, "fix: correct a regression")
	assert preview(root, source=promote(root))[1]["version"] == "2.0.1"


def test_major_three_is_rejected_before_any_release_commit_or_tag(release_repo):
	root = release_repo
	promote(root)
	local_release(root, bootstrap=True)
	published(root, "v1.2.5", "v2.0.0")
	git(root, "checkout", "nvg-develop")
	git(root, "merge", "--no-ff", "v2", "-m", "chore: synchronize published release")
	# Both a plain API response and gh --paginate --slurp are supported.
	api_page = json.loads((root / "published.json").read_text())[0]
	(root / "published.json").write_text(json.dumps(api_page))
	change(root, "feat!: change the public API incompatibly")
	source = promote(root)
	head = git(root, "rev-parse", "HEAD")
	result, values = preview(root, source=source, check=False)
	assert result.returncode == 1
	assert "outside stable major 2" in result.stderr
	assert not values
	assert git(root, "rev-parse", "HEAD") == head
	assert "v3.0.0" not in git(root, "tag", "--list")
	assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == "2.0.0"


def test_missing_history_and_unpublished_major_two_tags_are_rejected(release_repo):
	root = release_repo
	source = git(root, "rev-parse", "nvg-develop")
	git(root, "checkout", "v2")
	change(root, "chore: record a published release")
	git(root, "tag", "v1.2.6")
	published(root, "v1.2.5", "v1.2.6")
	result, _ = preview(root, source=source, check=False)
	assert result.returncode == 1
	assert "must include published release v1.2.6" in result.stderr
	assert "v1.30.1" in git(root, "tag", "--list")
	# A readable release list without an ancestral published tag must also fail clearly.
	published(root, "v1.2.7")
	result, _ = preview(root, check=False)
	assert result.returncode == 1
	assert "Published baseline v1.2.7 is not reachable" in result.stderr
	orphan = git(root, "commit-tree", "HEAD^{tree}", "-m", "chore: unrelated release history")
	git(root, "tag", "v1.2.7", orphan)
	result, _ = preview(root, check=False)
	assert result.returncode == 1
	assert "Published baseline v1.2.7 is not reachable" in result.stderr
	published(root, "v1.2.5")
	git(root, "tag", "v2.0.0")
	result, _ = preview(root, check=False)
	assert result.returncode == 1
	assert "Stable v2 tags already exist without a published v2 release" in result.stderr
