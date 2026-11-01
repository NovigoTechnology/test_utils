"""Compatibility entry point for the shared override tracker."""

import os
import sys

from test_utils.pre_commit import track_overrides as tracker
from test_utils.utils.track_overrides import UpstreamFetchError


def get_last_commit_hash_for_file_in_branch(repo_url, file_path):
	return tracker.get_last_commit_hash_for_file_in_branch(
		repo_url, file_path, os.getenv("GITHUB_BASE_REF") or "develop"
	)


def compare_commit_hashes(directory):
	changes, unverified = tracker.check_tracked_methods(directory, os.getenv("GITHUB_BASE_REF") or "develop")
	reports = [f"### {change['title']}\n\n```diff\n{change['diff']}\n```" for change in changes]
	if unverified:
		raise UpstreamFetchError("\n\n".join([*reports, *unverified]))
	return reports


if __name__ == "__main__":
	tracker.main(
		["--directory", sys.argv[1], "--base-branch", os.getenv("GITHUB_BASE_REF") or "develop", "--no-color"]
	)
