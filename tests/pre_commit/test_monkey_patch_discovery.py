import pytest
from test_validate_override_annotations import (
	annotation,
	function_source,
	hooks,
	validate,
	write,
)
from test_validate_override_annotations import (
	bench as bench,
)


def test_discovers_the_four_direct_nagus_monkey_patches_without_annotations(bench):
	write(
		bench / "frappe/frappe/core/doctype/session_default_settings/session_default_settings.py",
		"def clear_session_defaults(): pass\n",
	)
	write(
		bench / "erpnext/erpnext/setup/utils.py",
		"def get_exchange_rate(): pass\n",
	)
	write(
		bench / "erpnext/erpnext/controllers/trends.py",
		"def based_wise_columns_query(): pass\ndef get_data(): pass\n",
	)
	write(
		bench / "erpnext/erpnext/accounts/report/sales_invoice_trends/sales_invoice_trends.py",
		"from erpnext.controllers.trends import get_data\n",
	)
	write(bench / "nagus/nagus/api/user.py", "def onlogut(): pass\n")
	write(bench / "nagus/nagus/utils/utils.py", "def get_exchange_rate_nagus(): pass\n")
	write(
		bench / "nagus/nagus/controllers/trends.py",
		"def based_wise_columns_query(): pass\ndef get_data(): pass\n",
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import frappe.core.doctype.session_default_settings.session_default_settings
		import nagus.api.user
		frappe.core.doctype.session_default_settings.session_default_settings.clear_session_defaults = nagus.api.user.onlogut

		import erpnext.setup.utils
		from nagus.utils.utils import get_exchange_rate_nagus
		erpnext.setup.utils.get_exchange_rate = get_exchange_rate_nagus

		import erpnext.accounts.report.sales_invoice_trends.sales_invoice_trends
		import erpnext.controllers.trends
		import nagus.controllers.trends
		erpnext.controllers.trends.based_wise_columns_query = nagus.controllers.trends.based_wise_columns_query
		erpnext.accounts.report.sales_invoice_trends.sales_invoice_trends.get_data = nagus.controllers.trends.get_data
		""",
	)
	result = validate(bench)
	assert result.checked == 4
	assert len(result.errors) == 4
	assert all("Missing annotation fields" in error for error in result.errors)
	assert all("installed at" in error and "nagus/__init__.py" in error for error in result.errors)
	assert any("nagus.api.user.onlogut" in error for error in result.errors)
	assert any("nagus.utils.utils.get_exchange_rate_nagus" in error for error in result.errors)
	assert any("nagus.controllers.trends.based_wise_columns_query" in error for error in result.errors)
	assert any(
		"nagus.controllers.trends.get_data -> erpnext.controllers.trends.get_data" in error
		for error in result.errors
	)


def test_reexport_resolves_to_the_real_upstream_declaration(bench):
	write(bench / "erpnext/erpnext/controllers/trends.py", "def get_data(): pass\n")
	write(
		bench / "erpnext/erpnext/accounts/report/sales_invoice_trends/sales_invoice_trends.py",
		"from erpnext.controllers.trends import get_data\n",
	)
	write(
		bench / "nagus/nagus/controllers/trends.py",
		function_source(annotation(method="get_data", path="erpnext/controllers/trends.py"), name="get_data"),
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.report.sales_invoice_trends.sales_invoice_trends as report
		from nagus.controllers.trends import get_data
		report.get_data = get_data
		""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize(
	"imports,target,replacement",
	[
		(
			"import erpnext.accounts.utils as upstream\nfrom nagus.overrides import get_coa as custom",
			"upstream.get_coa",
			"custom",
		),
		(
			"from erpnext.accounts import utils as upstream\nimport nagus.overrides as custom",
			"upstream.get_coa",
			"custom.get_coa",
		),
	],
)
def test_imports_and_aliases_are_resolved(bench, imports, target, replacement):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		f"{imports}\n{target} = {replacement}\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_module_reexport_alias_is_resolved(bench):
	write(bench / "erpnext/erpnext/api.py", "def get_coa(): pass\n")
	write(bench / "erpnext/erpnext/__init__.py", "from . import api as public\n")
	write(
		bench / "nagus/nagus/overrides.py",
		function_source(annotation(path="erpnext/api.py")),
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"from erpnext import public\nfrom nagus.overrides import get_coa\npublic.get_coa = get_coa\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_missing_imported_owner_is_reported_instead_of_dropped(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"import erpnext.accounts.removed as upstream\n"
		"from nagus.overrides import get_coa\n"
		"upstream.get_coa = get_coa\n",
	)
	result = validate(bench)
	assert result.checked == 0
	assert len(result.errors) == 1
	assert "Cannot resolve monkey-patched owner module erpnext.accounts.removed" in result.errors[0]
	assert "source file not found" in result.errors[0]


def test_conditional_owner_does_not_abort_later_assignments(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		class Client: pass
		client = Client()
		from nagus.overrides import get_coa
		client.callback = get_coa
		import erpnext.accounts.utils as valid
		valid.get_coa = get_coa
		if enabled:
		    import erpnext.accounts.utils as conditional
		conditional.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert len(result.errors) == 1
	assert "conditional or unsupported binding of 'conditional'" in result.errors[0]
	assert "client.callback" not in result.errors[0]


def test_ambiguous_custom_namespace_retains_imported_owner_diagnostic(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"globals()\n"
		"import erpnext.accounts.utils as upstream\n"
		"from nagus.overrides import get_coa\n"
		"upstream.get_coa = get_coa\n",
	)
	result = validate(bench)
	assert result.checked == 0
	assert len(result.errors) == 1
	assert "Cannot resolve monkey-patched owner module erpnext.accounts.utils" in result.errors[0]
	assert "Ambiguous dynamic namespace" in result.errors[0]


def test_data_instance_class_and_nested_assignments_are_not_monkey_patches(bench):
	write(bench / "nagus/nagus/overrides.py", function_source())
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as upstream
		import frappe
		from nagus.overrides import get_coa
		upstream.enabled = True
		frappe.flags.feature = True
		upstream.Controller.validate = get_coa
		def install_later():
			upstream.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 0
	assert not result.errors


def test_removed_original_is_reported_with_assignment_and_replacement(bench):
	write(bench / "erpnext/erpnext/accounts/utils.py", "def renamed(): pass\n")
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as upstream
		from nagus.overrides import get_coa
		upstream.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 0
	assert len(result.errors) == 1
	assert "nagus.overrides.get_coa -> erpnext.accounts.utils.get_coa" in result.errors[0]
	assert "removed or renamed" in result.errors[0]
	assert "nagus/__init__.py:3" in result.errors[0]


def test_ambiguous_original_and_unresolved_replacement_are_reported(bench):
	write(
		bench / "erpnext/erpnext/accounts/utils.py",
		"globals()\ndef get_coa(): pass\n",
	)
	write(
		bench / "frappe/frappe/core/doctype/session_default_settings/session_default_settings.py",
		"def clear_session_defaults(): pass\n",
	)
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as upstream
		import frappe.core.doctype.session_default_settings.session_default_settings as session_defaults
		from nagus.overrides import get_coa
		upstream.get_coa = get_coa
		session_defaults.clear_session_defaults = missing_replacement
		""",
	)
	result = validate(bench)
	assert result.checked == 0
	assert len(result.errors) == 2
	assert any("Ambiguous dynamic namespace" in error for error in result.errors)
	assert any("Cannot resolve replacement function" in error for error in result.errors)
	assert all("nagus/__init__.py" in error for error in result.errors)


def test_same_pair_registered_by_hook_and_assignment_is_validated_once(bench):
	hooks(
		bench,
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}",
	)
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as upstream
		from nagus.overrides import get_coa
		upstream.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_assignment_in_a_non_init_module_is_discovered_without_importing_it(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/install.py",
		"""
		raise RuntimeError("must not be imported")
		import erpnext.accounts.utils as upstream
		from nagus.overrides import get_coa
		upstream.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors
