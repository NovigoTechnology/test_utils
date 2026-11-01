"""Check promotion provenance, publication retries and idempotent PR creation."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from test_release_version import change, git, published
from test_release_version import release_repo as release_repo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/scripts"))
import release_flow

REPOSITORY = "NovigoTechnology/test_utils"


def event(merge="a" * 40, source="b" * 40):
	return {
		"action": "closed",
		"pull_request": {
			"merged": True,
			"merge_commit_sha": merge,
			"base": {"ref": "v2", "repo": {"full_name": REPOSITORY}},
			"head": {"ref": "nvg-develop", "sha": source, "repo": {"full_name": REPOSITORY}},
		},
	}


def test_only_the_reviewed_development_branch_can_promote():
	valid = event()
	assert release_flow.promotion(valid, REPOSITORY) is valid["pull_request"]
	for key, field, value in (
		("head", "ref", "feature/unreviewed"),
		("base", "ref", "nvg-develop"),
		("head", "repo", {"full_name": "a-fork/test_utils"}),
	):
		invalid = deepcopy(valid)
		invalid["pull_request"][key][field] = value
		with pytest.raises(ValueError, match="promotions must"):
			release_flow.promotion(invalid, REPOSITORY)


def test_publish_checks_merge_parents_and_accepts_retry_after_publication(release_repo, monkeypatch):
	root = release_repo
	monkeypatch.chdir(root)
	change(root, "fix: release a reviewed fix")
	source = git(root, "rev-parse", "HEAD")
	git(root, "switch", "v2")
	git(root, "merge", "--no-ff", "nvg-develop", "-m", "chore: promote reviewed changes")
	merge = git(root, "rev-parse", "HEAD")
	valid = event(merge, source)
	releases = published(root, "v1.2.5")
	assert release_flow.validate_publication(valid, REPOSITORY, releases) is False
	with pytest.raises(ValueError, match="merge commit"):
		release_flow.validate_publication(event(merge, "c" * 40), REPOSITORY, releases)
	closed = deepcopy(valid)
	closed["pull_request"]["merged"] = False
	with pytest.raises(ValueError, match="merged promotion"):
		release_flow.validate_publication(closed, REPOSITORY, releases)
	# A publication adds the version commit. Its retry only needs the return PR.
	change(root, "chore(release): 2.0.0")
	git(root, "tag", "v2.0.0")
	releases = published(root, "v1.2.5", "v2.0.0")
	assert release_flow.validate_publication(valid, REPOSITORY, releases) is True
	change(root, "docs: unrelated later changes")
	with pytest.raises(ValueError, match="advanced beyond"):
		release_flow.validate_publication(valid, REPOSITORY, releases)


def test_mergeback_reuses_an_open_pr_and_stops_after_synchronization(tmp_path, monkeypatch):
	calls = []
	responses = iter(({"ahead_by": 1}, [{"url": "https://github.com/example/repo/pull/1"}], ""))

	def command(*args):
		calls.append(args)
		response = next(responses)
		return CompletedProcess(
			args, 0, json.dumps(response) if not isinstance(response, str) else response, ""
		)

	monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
	monkeypatch.setattr(release_flow, "command", command)
	release_flow.open_pr(REPOSITORY, "mergeback")
	assert calls[0][-1].endswith("/compare/nvg-develop...v2")
	assert any(call[:3] == ("gh", "pr", "edit") for call in calls)
	assert not any(call[:3] == ("gh", "pr", "create") for call in calls)
	assert "Create a merge commit" in (tmp_path / "test-utils-release-pr.md").read_text()
	calls.clear()
	responses = iter(({"ahead_by": 0},))
	release_flow.open_pr(REPOSITORY, "mergeback")
	assert len(calls) == 1
