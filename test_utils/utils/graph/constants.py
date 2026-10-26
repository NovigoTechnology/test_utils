"""Shared path filters for scanning app trees."""

SKIP_DIR_PARTS: frozenset[str] = frozenset(
	{
		"__pycache__",
		"node_modules",
		".git",
		".venv",
		"venv",
		"site-packages",
		".mypy_cache",
		".pytest_cache",
		"dist",
		"build",
	}
)
