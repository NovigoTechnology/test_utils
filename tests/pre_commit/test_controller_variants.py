"""Resolve statically declared controller alternatives without running application code."""

import builtins
import textwrap

from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench

PACKAGE = "erpnext/accounts/doctype/ledger"
CONTROLLER = """from frappe.model.document import Document

def feature():
    raise AssertionError("Controller selection must not execute")

class Ledger(Document):
    def validate(self):
        return "base"

    @classmethod
    def resolve_controller(cls):
        from .legacy import LegacyLedger
        from .modern import ModernLedger
        if issubclass(cls, LegacyLedger | ModernLedger):
            return cls
        concrete = ModernLedger if feature() else LegacyLedger
        if cls is Ledger:
            return concrete
        return type(cls.__name__, (cls, concrete), {"__module__": cls.__module__})
"""


def controllers(bench, *, legacy_method="validate_duplicate_entry", modern_method=None):
	write(bench / "erpnext" / PACKAGE / "ledger.py", CONTROLLER)
	for module, owner, method in (
		("legacy", "LegacyLedger", legacy_method),
		("modern", "ModernLedger", modern_method),
	):
		body = function_source(name=method, indent="    ") if method else "    pass\n"
		write(
			bench / "erpnext" / PACKAGE / f"{module}.py",
			f"from .ledger import Ledger\nclass {owner}(Ledger):\n" + body,
		)


def backend_annotation(owner="LegacyLedger", module="legacy", method="validate_duplicate_entry"):
	return annotation(method=f"{owner}.{method}", path=f"{PACKAGE}/{module}.py")


def mixin(bench, doc="", *, method="validate_duplicate_entry"):
	register_class(
		bench,
		"class CustomLedger:\n" + function_source(doc, name=method, indent="    "),
		hook="extend_doctype_class",
	)


def test_lazy_controller_variant_annotation_passes_without_importing_apps(bench, monkeypatch):
	controllers(bench)
	mixin(bench, backend_annotation())
	original_import = builtins.__import__

	def guarded_import(name, *args, **kwargs):
		assert name.partition(".")[0] not in {"nagus", "frappe", "erpnext", "nvg_base"}, name
		return original_import(name, *args, **kwargs)

	monkeypatch.setattr(builtins, "__import__", guarded_import)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_optional_controller_method_requires_annotation(bench):
	controllers(bench)
	mixin(bench)
	result = validate(bench)
	assert result.checked == 1
	assert len(result.errors) == 1
	assert "Missing annotation fields: APP, HASH, REPO, PATH, METHOD" in result.errors[0]
	assert "LegacyLedger.validate_duplicate_entry" in result.errors[0]


def test_optional_controller_bindings_are_not_discarded_when_the_callable_is_unresolved(bench):
	method = function_source(name="validate_duplicate_entry")
	alias = "validate_duplicate_entry = callback\n"
	conditional = "if enabled():\n" + textwrap.indent(method, "    ")
	cases = (
		("callable alias", alias, "pass\n", method, True),
		("conditional declaration", conditional, "pass\n", method, True),
		(
			"unresolved alternative alongside annotated implementation",
			method,
			alias,
			function_source(backend_annotation(), name="validate_duplicate_entry"),
			True,
		),
		(
			"ordinary data",
			"validate_duplicate_entry = 1\n",
			"pass\n",
			"validate_duplicate_entry = 2\n",
			False,
		),
		(
			"data in only one alternative",
			method,
			"validate_duplicate_entry = 1\n",
			"validate_duplicate_entry = 2\n",
			True,
		),
	)
	for label, legacy, modern, custom, invalid in cases:
		controllers(bench)
		for module, owner, body in (("legacy", "LegacyLedger", legacy), ("modern", "ModernLedger", modern)):
			write(
				bench / "erpnext" / PACKAGE / f"{module}.py",
				"from .ledger import Ledger\n"
				+ function_source(name="callback")
				+ f"class {owner}(Ledger):\n"
				+ textwrap.indent(body, "    "),
			)
		register_class(
			bench,
			"class CustomLedger:\n" + textwrap.indent(custom, "    "),
			hook="extend_doctype_class",
		)
		result = validate(bench)
		assert bool(result.errors) is invalid, (label, result.errors)
		if invalid:
			assert any("Cannot resolve concrete implementation" in error for error in result.errors), (
				label,
				result.errors,
			)


def test_removed_controller_variant_method_remains_an_error(bench):
	controllers(bench, legacy_method=None)
	mixin(bench, backend_annotation())
	result = validate(bench)
	assert result.errors
	assert any("no longer exists" in error or "not found" in error for error in result.errors)


def test_imported_unselected_or_unrelated_classes_do_not_become_controller_variants(bench):
	controllers(bench)
	write(
		bench / "erpnext" / PACKAGE / "outsiders.py",
		"from .ledger import Ledger\nclass UnrelatedLedger:\n"
		+ function_source(name="validate_duplicate_entry", indent="    ")
		+ "class UnselectedLedger(Ledger):\n"
		+ function_source(name="validate_duplicate_entry", indent="    "),
	)
	controller = bench / "erpnext" / PACKAGE / "ledger.py"
	controller.write_text(
		CONTROLLER.replace(
			"from .modern import ModernLedger",
			"from .modern import ModernLedger\n        from .outsiders import UnrelatedLedger, UnselectedLedger",
		)
	)
	for owner in ("UnrelatedLedger", "UnselectedLedger"):
		mixin(bench, backend_annotation(owner, "outsiders"))
		assert validate(bench).errors, owner
	# An unsupported selection callback must not turn every imported subclass into a candidate.
	controller.write_text(
		CONTROLLER[: CONTROLLER.index("        if issubclass")] + "        return select_controller()\n"
	)
	mixin(bench, backend_annotation())
	assert validate(bench).errors


def test_multiple_variant_implementations_require_unambiguous_method_metadata(bench):
	controllers(bench, modern_method="validate_duplicate_entry")
	write(
		bench / "erpnext" / PACKAGE / "backends.py",
		"from .ledger import Ledger\nclass LegacyLedger(Ledger):\n"
		+ function_source(name="validate_duplicate_entry", indent="    ")
		+ "class ModernLedger(Ledger):\n"
		+ function_source(name="validate_duplicate_entry", indent="    "),
	)
	write(
		bench / "erpnext" / PACKAGE / "ledger.py",
		CONTROLLER.replace(
			"from .legacy import LegacyLedger\n        from .modern import ModernLedger",
			"from .backends import LegacyLedger, ModernLedger",
		),
	)
	mixin(
		bench,
		annotation(method="validate_duplicate_entry", path=f"{PACKAGE}/backends.py"),
	)
	result = validate(bench)
	assert result.errors
	assert any("METHOD" in error for error in result.errors)
	for owner in ("LegacyLedger", "ModernLedger"):
		mixin(bench, backend_annotation(owner, "backends"))
		result = validate(bench)
		assert result.checked == 1, owner
		assert not result.errors, (owner, result.errors)


def test_inherited_backend_method_keeps_base_controller_as_its_annotation_owner(bench):
	controllers(bench)
	mixin(bench, backend_annotation("Ledger", "ledger", "validate"), method="validate")
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_unavailable_imports_receivers_and_return_values_fail_closed(bench):
	controllers(bench)
	mixin(bench, backend_annotation())
	prefix = CONTROLLER[: CONTROLLER.index("        from .legacy")]
	cases = {
		"mixed dynamic return": (
			"from .legacy import LegacyLedger\nreturn LegacyLedger if feature() else choose_controller()\n"
		),
		"early None return": "return None\nfrom .legacy import LegacyLedger\nreturn LegacyLedger\n",
		"import after return": "return LegacyLedger\nfrom .legacy import LegacyLedger\n",
		"unreachable import": "if False:\n    from .legacy import LegacyLedger\nreturn LegacyLedger\n",
		"reassigned receiver": "cls = Ledger\nfrom .legacy import LegacyLedger\nreturn LegacyLedger\n",
		"implicit None return": "from .legacy import LegacyLedger\nif feature():\n    return LegacyLedger\n",
		"union return": "from .legacy import LegacyLedger\nfrom .modern import ModernLedger\nreturn LegacyLedger | ModernLedger\n",
		"conditional subclass guard": (
			"from .legacy import LegacyLedger\nfrom .modern import ModernLedger\n"
			"if issubclass(cls, ModernLedger if feature() else LegacyLedger):\n    return cls\n"
			"return LegacyLedger\n"
		),
	}
	cases = {name: prefix + textwrap.indent(body, "        ") for name, body in cases.items()}
	cases["extra formal parameter"] = prefix.replace(
		"resolve_controller(cls)", "resolve_controller(cls, LegacyLedger=object)"
	) + textwrap.indent("from .legacy import LegacyLedger\nreturn LegacyLedger\n", "        ")
	cases["async callback"] = prefix.replace(
		"def resolve_controller", "async def resolve_controller"
	) + textwrap.indent("from .legacy import LegacyLedger\nreturn LegacyLedger\n", "        ")
	cases["generator callback"] = prefix + textwrap.indent(
		"from .legacy import LegacyLedger\nyield LegacyLedger\nreturn LegacyLedger\n", "        "
	)
	for name, source in cases.items():
		write(bench / "erpnext" / PACKAGE / "ledger.py", source)
		assert validate(bench).errors, name


def test_concrete_and_local_controllers_do_not_borrow_unrelated_variant_metadata(bench):
	controllers(bench, modern_method="validate_duplicate_entry")
	for owner, module, invalid in (("ModernLedger", "modern", True), ("LegacyLedger", "legacy", False)):
		register_class(
			bench,
			"from erpnext.accounts.doctype.ledger.legacy import LegacyLedger\n"
			"class CustomLedger(LegacyLedger):\n"
			+ function_source(
				backend_annotation(owner, module), name="validate_duplicate_entry", indent="    "
			),
		)
		result = validate(bench)
		assert bool(result.errors) is invalid, (owner, result.errors)
	write(
		bench / "erpnext" / PACKAGE / "ledger.py",
		CONTROLLER.replace("LegacyLedger | ModernLedger", "LegacyLedger | UnknownController"),
	)
	assert validate(bench).errors
	# A factory in the custom app does not turn its own helper into an upstream override.
	write(bench / "erpnext" / PACKAGE / "ledger.py", CONTROLLER[: CONTROLLER.index("    @classmethod")])
	write(
		bench / "nagus/nagus/backends.py",
		"from .overrides import CustomLedger\nclass LocalLedger(CustomLedger):\n"
		+ function_source(name="validate_duplicate_entry", indent="    "),
	)
	local_metadata = annotation(
		method="LocalLedger.validate_duplicate_entry",
		path="nagus/backends.py",
		repo="https://github.com/NovigoTechnology/nagus",
	)
	for doc, invalid in ((local_metadata, True), ("", False)):
		register_class(
			bench,
			"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
			"class CustomLedger(Ledger):\n"
			"    @classmethod\n"
			"    def resolve_controller(cls):\n"
			"        from .backends import LocalLedger\n"
			"        return LocalLedger\n"
			+ function_source(doc, name="validate_duplicate_entry", indent="    "),
		)
		result = validate(bench)
		assert bool(result.errors) is invalid, result.errors
		assert result.checked == 0
