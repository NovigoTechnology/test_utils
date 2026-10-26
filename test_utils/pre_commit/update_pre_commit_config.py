import subprocess
import sys


def main():
	try:
		result = subprocess.run(
			["pre-commit", "autoupdate", "--repo", "https://github.com/NovigoTechnology/test_utils"],
			capture_output=True,
			text=True,
		)
	except OSError as e:
		print(f"Error: {e}", file=sys.stderr)
		sys.exit(1)

	sys.stdout.write(result.stdout)
	sys.stderr.write(result.stderr)
	sys.exit(result.returncode)


if __name__ == "__main__":
	main()
