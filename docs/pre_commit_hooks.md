## Using test_utils with pre-commit

Replace `<tag-or-commit>` with a published Novigo tag or full commit SHA containing the tools you need.

Add this to your .pre-commit-config.yaml

### Hooks available

### Validate Copyright - `validate_copyright`

Check JavaScript, TypeScript, Python, Markdown, HTML, and SQL files. Add or refresh the copyright header while preserving file contents.

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_copyright
        files: '\.(js|ts|py|md|html|sql)$'
        args: ["--app", "{app_name}"]
```

In the Novigo fork, `--app` is optional: the hook reads the app name from `[project].name` or `[tool.poetry].name` in `pyproject.toml`. Existing configurations with `--app` remain supported. Python callers can use either `validate_copyright(files)` or `validate_copyright(app, files)`; the latter also accepts a dictionary with `app_publisher`.

The hook inserts the header after a shebang and Python encoding declaration, preserving their positions. It preserves source encoding, UTF-8 BOM, original line endings, file permissions, and body contents. Repeated runs leave an unchanged file untouched.

### Clean customized doctypes - `clean_customized_doctypes`

Remove unused keys in customizations.

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: clean_customized_doctypes
        args: ["--app", "{app_name}"]
```

### Validate javascript dependencies - `validate_javascript_dependencies`

Examine package.json across the installed apps on the site to detect any version mismatches

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_javascript_dependencies
```

### Validate python dependencies - `validate_python_dependencies`

Examine pyproject.toml across the installed apps on the site to detect any version mismatches

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_python_dependencies
```

### Validate customizations - `validate_customizations`

Validate Customizations

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_customizations
```

### Update pre-commit config - `update_pre_commit_config`

Update the Novigo test_utils entry in `.pre-commit-config.yaml` to the latest revision using `pre-commit autoupdate --repo https://github.com/NovigoTechnology/test_utils`. The hook forwards stdout, stderr, and the process exit code. If `pre-commit` cannot be started, it reports the error on stderr and exits with code 1.

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: update_pre_commit_config
```

### Mypy - `mypy`

Run mypy on the codebase using a preset configuration

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: mypy
```

### Override annotations - `validate_override_annotations`

Discover overrides in `override_whitelisted_methods`, `override_doctype_class`, and
`extend_doctype_class` using AST, then require valid upstream metadata in each overriding
callable's docstring. Dependencies from `required_apps`, plus Frappe, must be checked out
under `--apps-root`. Full reference: [`validate_override_annotations.md`](validate_override_annotations.md).

```yaml
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_override_annotations
        args: ["--app", "nagus", "--apps-root", ".."]
```

Use alongside `track_overrides`: annotation validation detects untracked overrides;
tracking checks upstream drift for annotated overrides.

### DocType Python types - `validate_doctype_python_types`

Ensures DocType controllers include Frappe’s auto-generated `TYPE_CHECKING` block or a documented opt-out comment when `export_python_type_annotations` is enabled. Full reference: [`validate_doctype_python_types.md`](validate_doctype_python_types.md).

```
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_doctype_python_types
        args: ["--app", "."]
```

Add `"--interactive"` to `args` locally if you want prompts to run `export_types_to_controller` or insert the skip comment; use `"--force"` to enforce before enabling the hooks flag.
