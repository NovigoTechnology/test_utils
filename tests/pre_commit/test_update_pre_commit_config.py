import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_utils.pre_commit import update_pre_commit_config


@pytest.mark.parametrize("returncode", [0, 1, 3])
def test_cli_updates_novigo_and_forwards_output_and_exit_code(tmp_path, returncode):
	command = tmp_path / "pre-commit"
	command.write_text(
		f"#!{sys.executable}\n"
		"import json, sys\n"
		"print(json.dumps(sys.argv[1:]))\n"
		"sys.stderr.write('update diagnostic\\n')\n"
		f"sys.exit({returncode})\n"
	)
	command.chmod(0o755)
	result = run_updater(tmp_path)
	assert result.returncode == returncode
	assert (
		result.stdout
		== json.dumps(["autoupdate", "--repo", "https://github.com/NovigoTechnology/test_utils"]) + "\n"
	)
	assert result.stderr == "update diagnostic\n"


def test_cli_reports_missing_pre_commit_as_failure(tmp_path):
	result = run_updater(tmp_path)
	assert result.returncode == 1
	assert result.stdout == ""
	assert "Error:" in result.stderr
	assert "pre-commit" in result.stderr


def run_updater(command_dir):
	environment = os.environ.copy()
	environment["PATH"] = str(command_dir)
	source_root = str(Path(update_pre_commit_config.__file__).resolve().parents[2])
	environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, environment.get("PYTHONPATH")]))
	return subprocess.run(
		[sys.executable, "-m", "test_utils.pre_commit.update_pre_commit_config"],
		cwd=command_dir,
		env=environment,
		capture_output=True,
		text=True,
		timeout=10,
	)
