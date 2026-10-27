"""Pre-commit entry point for static Frappe override annotation validation."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from test_utils.utils.override_annotations import validate_overrides


def main(argv: Sequence[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Require complete upstream annotations on Frappe overrides.")
	parser.add_argument(
		"--app", default=".", help="App repository/package path, or app name under --apps-root (default: cwd)"
	)
	parser.add_argument(
		"--apps-root",
		type=Path,
		help="Directory containing dependency checkouts (default: sibling app repositories)",
	)
	args = parser.parse_args(argv)
	app_path = Path(args.app)
	if not app_path.exists() and args.apps_root:
		app_path = args.apps_root / args.app
	result = validate_overrides(app_path, args.apps_root)
	for error in result.errors:
		print(error, file=sys.stderr)
	print(
		f"validate_override_annotations: checked {result.checked} override(s), {len(result.errors)} error(s)"
	)
	return 1 if result.errors else 0


if __name__ == "__main__":
	sys.exit(main())
