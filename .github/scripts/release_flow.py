"""Validate reviewed promotions and open the pull requests in the release circuit."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

from release_version import command, is_ancestor, published_tags

DEVELOPMENT = "nvg-develop"
STABLE = "v2"


def promotion(event, repository):
	pr = event["pull_request"]
	if (
		pr["base"]["ref"] != STABLE
		or pr["head"]["ref"] != DEVELOPMENT
		or pr["head"]["repo"]["full_name"] != repository
		or pr["base"]["repo"]["full_name"] != repository
	):
		raise ValueError("Release promotions must be nvg-develop -> v2 within this repository")
	return pr


def validate_publication(event, repository, releases):
	pr = promotion(event, repository)
	if event.get("action") != "closed" or not pr.get("merged"):
		raise ValueError("Publication requires a merged promotion pull request")
	merge_sha, source_sha = pr["merge_commit_sha"], pr["head"]["sha"]
	if not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") for sha in (merge_sha, source_sha)):
		raise ValueError("The promotion must identify full merge and source commit SHAs")
	current = command("git", "rev-parse", "HEAD").stdout.strip()
	if current != merge_sha:
		tags = set(command("git", "tag", "--points-at", "HEAD").stdout.splitlines())
		stable_tags = {tag for tag in published_tags(releases) if tag.startswith("v2.")}
		if tags.intersection(stable_tags) and is_ancestor(merge_sha, current):
			return True
		raise ValueError("v2 advanced beyond this promotion; use the latest promotion run")
	parents = command("git", "show", "-s", "--format=%P", current).stdout.split()
	if len(parents) != 2 or parents[1] != source_sha:
		raise ValueError("Promote with Create a merge commit, preserving the approved development head")
	return False


def open_pr(repository, direction, version=None):
	head, base = (DEVELOPMENT, STABLE) if direction == "promotion" else (STABLE, DEVELOPMENT)
	comparison = json.loads(command("gh", "api", f"repos/{repository}/compare/{base}...{head}").stdout)
	if comparison["ahead_by"] == 0:
		print(f"{base} already contains {head}; no pull request needed")
		return
	if direction == "promotion" and (version is None or not re.fullmatch(r"2\.[0-9]+\.[0-9]+", version)):
		raise ValueError("The release PR must identify a stable major-2 version")
	title = f"chore(release): {'promote ' + version if version else 'synchronize v2'}"
	body = (
		f"Promote `{head}` into `{base}`.\n\n"
		"Use **Create a merge commit** so published tags and the original commits remain in history.\n\n"
		"Wait for all required checks and approve the workflows if GitHub requests it.\n"
	)
	if direction == "promotion":
		body += f"\nExpected release: **{version}**. The Release policy check recalculates it for the current PR head.\n"
	else:
		body += "\nThis returns release metadata and tag ancestry to development. It does not publish another release.\n"
	prs = json.loads(command("gh", "pr", "list", "--repo", repository, "--base", base, "--head", head,
		"--state", "open", "--json", "url").stdout)
	body_file = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "test-utils-release-pr.md"
	body_file.write_text(body, encoding="utf-8")
	if prs:
		url = prs[0]["url"]
		command("gh", "pr", "edit", url, "--title", title, "--body-file", str(body_file))
	else:
		url = command("gh", "pr", "create", "--repo", repository, "--base", base, "--head", head,
			"--title", title, "--body-file", str(body_file)).stdout.strip()
	print(url)
	if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
		with Path(summary).open("a", encoding="utf-8") as stream:
			stream.write(f"\nPull request: {url}\n")


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("operation", choices=("validate", "publish-context", "open-promotion", "open-mergeback"))
	parser.add_argument("--event", type=Path)
	parser.add_argument("--published-releases", type=Path)
	parser.add_argument("--version")
	args = parser.parse_args()
	try:
		repository = os.environ["GITHUB_REPOSITORY"]
		if args.operation.startswith("open-"):
			open_pr(repository, "promotion" if args.operation == "open-promotion" else "mergeback", args.version)
		else:
			event = json.loads(args.event.read_text(encoding="utf-8"))
			if args.operation == "validate":
				promotion(event, repository)
			else:
				already = validate_publication(event, repository, args.published_releases)
				if output := os.environ.get("GITHUB_OUTPUT"):
					with Path(output).open("a", encoding="utf-8") as stream:
						stream.write(f"already_published={str(already).lower()}\n")
	except (KeyError, TypeError, AttributeError, OSError, ValueError) as error:
		print(f"release_flow: {error}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
