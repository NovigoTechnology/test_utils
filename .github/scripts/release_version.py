"""Calculate a stable v2 release in a disposable CI clone; never modify Git refs."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


STABLE_TAG = re.compile(r"v[12]\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def command(*args, allowed=(0,), env=None):
	result = subprocess.run(args, text=True, capture_output=True, env=env)
	if result.returncode not in allowed:
		raise ValueError(result.stderr.strip() or f"Command failed: {args[0]}")
	return result


def version_tuple(tag):
	return tuple(int(part) for part in tag.removeprefix("v").split("."))


def published_tags(path):
	payload = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(payload, list):
		raise ValueError("Published releases must be a JSON array or an array of paginated arrays")
	releases = []
	for page in payload:
		releases.extend(page if isinstance(page, list) else [page])
	if any(not isinstance(release, dict) for release in releases):
		raise ValueError("Each published release must be a JSON object")
	return {
		release["tag_name"]
		for release in releases
		if isinstance(release.get("tag_name"), str)
		and STABLE_TAG.fullmatch(release["tag_name"])
		and release.get("published_at")
		and not release.get("draft")
		and not release.get("prerelease")
	}


def is_ancestor(tag, commit):
	return command("git", "merge-base", "--is-ancestor", tag, commit, allowed=(0, 1)).returncode == 0


def calculate(releases_path, source_sha=None):
	published = published_tags(releases_path)
	local_tags = set(command("git", "tag", "--list").stdout.splitlines())
	published_v2 = {tag for tag in published if tag.startswith("v2.")}
	bootstrap = not published_v2
	if bootstrap and any(STABLE_TAG.fullmatch(tag) and tag.startswith("v2.") for tag in local_tags):
		raise ValueError("Stable v2 tags already exist without a published v2 release; reconcile that release before bootstrapping")
	baselines = published_v2 or published
	if not baselines:
		raise ValueError("No published stable release is available as a baseline")
	baseline = max(baselines, key=version_tuple)
	if baseline not in local_tags or not is_ancestor(baseline, "HEAD"):
		raise ValueError(f"Published baseline {baseline} is not reachable from HEAD; fetch and merge the release history first")
	if source_sha:
		source = command("git", "rev-parse", "--verify", "--end-of-options", f"{source_sha}^{{commit}}").stdout.strip()
		if not is_ancestor(baseline, source):
			raise ValueError(f"Source branch must include published release {baseline}; merge its synchronization PR first")
	# Force the first major-2 release once, leaving imported major-1 tags intact.
	arguments = ["semantic-release", "--noop", "version", "--print"]
	if bootstrap:
		arguments.append("--major")
	version = command(
		*arguments,
		env={name: value for name, value in os.environ.items() if name != "GITHUB_OUTPUT"},
	).stdout.strip()
	tag = f"v{version}"
	if not STABLE_TAG.fullmatch(tag) or not tag.startswith("v2."):
		raise ValueError(f"Calculated version {version!r} is outside stable major 2; use a separate major release branch")
	if bootstrap and version != "2.0.0":
		raise ValueError(f"First stable major-2 release must be 2.0.0, not {version}")
	if version_tuple(tag) < version_tuple(baseline):
		raise ValueError(f"Calculated version {version} is older than published release {baseline}")
	return {
		"baseline": baseline.removeprefix("v"),
		"version": version,
		"tag": tag,
		"changed": str(tag != baseline).lower(),
		"bootstrap": str(bootstrap).lower(),
	}


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--published-releases", required=True, type=Path)
	parser.add_argument("--source-sha", help="Development head that must already include the published baseline")
	args = parser.parse_args()
	try:
		values = calculate(args.published_releases, args.source_sha)
		output = "".join(f"{name}={value}\n" for name, value in values.items())
		if output_path := os.environ.get("GITHUB_OUTPUT"):
			with Path(output_path).open("a", encoding="utf-8") as stream:
				stream.write(output)
		print(output, end="")
	except (OSError, ValueError) as error:
		print(f"release_version: {error}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
