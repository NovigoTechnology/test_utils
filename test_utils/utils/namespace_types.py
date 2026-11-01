"""Prove safe namespace values using a closed set of source shapes, without imports.

Supported values are literals, functions, standard function descriptors, and inert
local instances. Decorators must return a local function or preserve their function
argument, optionally through another source function with the same return proof.
"""

import ast

from test_utils.utils.python_source import (
	executable_nodes,
	runtime_body,
	scope_bindings,
	scope_statements,
	unique_definition,
)


def class_values_are_safe(resolver, module, owner):
	"""Return False whenever an enumerated value could customize ``__class__`` lookup."""
	checking = set()
	callable_state = {"__code__", "__defaults__", "__kwdefaults__", "__annotations__"}
	lexical_cache = {}

	def function_scopes(provider, body):
		key = (provider.path, id(body))
		if key not in lexical_cache:
			parents = {
				child: parent for parent in ast.walk(provider.tree) for child in ast.iter_child_nodes(parent)
			}
			node = next(
				(
					n
					for n in parents
					if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body is body
				),
				None,
			)
			scopes = []
			while node is not None:
				if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
					scopes.append(node)
				node = parents.get(node)
			lexical_cache[key] = scopes
		return lexical_cache[key]

	def namespace_binding(name, provider, body):
		"""Select a Python namespace; None declarations mean its value is unverified."""
		functions = function_scopes(provider, body)
		scopes = [(function.body, function) for function in functions]
		if not functions or functions[0].body is not body:
			scopes.insert(0, (body, None))
		module_body = runtime_body(provider.tree)
		scopes.append((module_body, None))
		nonlocal_name = False
		index = 0
		while index < len(scopes):
			scope, function = scopes[index]
			index += 1
			if nonlocal_name and scope is module_body:
				return scope, None
			bindings = scope_bindings(scope)
			declarations = list(scope_statements(scope))
			global_name = any(isinstance(s, ast.Global) and name in s.names for s in declarations)
			outer_name = any(isinstance(s, ast.Nonlocal) and name in s.names for s in declarations)
			if scope is module_body:
				global_name = False
			if global_name or outer_name:
				if name in bindings or (nonlocal_name and global_name):
					return scope, None
				if global_name:
					index = len(scopes) - 1
				else:
					nonlocal_name = True
				continue
			if "*" in bindings:
				return scope, None
			if name in bindings:
				return scope, bindings[name]
			parameters = (
				[]
				if function is None
				else [
					*function.args.posonlyargs,
					*function.args.args,
					*function.args.kwonlyargs,
					function.args.vararg,
					function.args.kwarg,
				]
			)
			if any(p and p.arg == name for p in parameters) or (
				function is not None
				and any(
					isinstance(s, ast.AnnAssign)
					and s.value is None
					and isinstance(s.target, ast.Name)
					and s.target.id == name
					for s in declarations
				)
			):
				return scope, None
		return None, []

	def namespace_safe(expression, provider, local_body, names):
		deferred = bool(function_scopes(provider, local_body))
		prefix = runtime_body(provider.tree)
		if not deferred:
			prefix = [statement for statement in prefix if statement.lineno < expression.lineno]
		local_prefix = [s for s in local_body if s.lineno < expression.lineno]
		closures = [
			function.body
			for function in function_scopes(provider, local_body)
			if function.body is not local_body
		]
		if deferred:
			mutation_scopes = [local_body, *closures]
			mutation_scopes += [
				node.body
				for body in [local_body, *closures]
				for statement in body
				for node in ast.walk(statement)
				if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
			]
			if any(
				isinstance(statement, (ast.Global, ast.Nonlocal))
				and set(statement.names).intersection(names, scope_bindings(body))
				for body in mutation_scopes
				for statement in scope_statements(body)
			):
				return False
		return not resolver.dynamic_namespace(
			[prefix, local_prefix, *closures], names, provider.tree, module=provider
		)

	def reference(expression, provider, local_body=()):
		original = expression
		parts = []
		while isinstance(expression, ast.Attribute):
			parts.insert(0, expression.attr)
			expression = expression.value
		if not isinstance(expression, ast.Name):
			return None
		name = expression.id
		if not namespace_safe(
			original, provider, local_body, {name, *parts, "__code__", "__defaults__", "__kwdefaults__"}
		):
			return None
		body, definitions = namespace_binding(name, provider, local_body)
		if definitions is None or len(definitions) != 1 or not isinstance(definitions[0], ast.alias):
			return None
		alias = definitions[0]
		statement = next(
			(s for s in body if isinstance(s, (ast.Import, ast.ImportFrom)) and alias in s.names), None
		)
		if statement is None or (body is local_body and statement.lineno >= original.lineno):
			return None
		if not function_scopes(provider, local_body) and statement.lineno >= original.lineno:
			return None
		prefix = provider.import_reference(statement, alias)
		return ".".join([prefix, *parts])

	def builtin(name, provider, local_body, expression):
		body, _ = namespace_binding(name, provider, local_body)
		return body is None and namespace_safe(expression, provider, local_body, {name})

	def function_descriptor(function, provider, local_body=(), seen=frozenset()):
		kind = "function"
		for decorator in reversed(function.decorator_list):
			if (
				isinstance(decorator, ast.Name)
				and decorator.id in {"property", "staticmethod", "classmethod"}
				and builtin(decorator.id, provider, local_body, decorator)
			):
				kind = "descriptor"
				continue
			if reference(decorator, provider, local_body) == "functools.cached_property":
				kind = "descriptor"
				continue
			if (
				isinstance(decorator, ast.Call)
				and reference(decorator.func, provider, local_body) == "functools.wraps"
			):
				if kind != "function":
					return None
				continue
			if kind != "function":
				return None
			try:
				decorator_symbol = resolver.resolve_registration(
					reference(
						decorator.func if isinstance(decorator, ast.Call) else decorator, provider, local_body
					)
					or ""
				)
			except ValueError:
				return None
			if isinstance(decorator, ast.Call):
				returned = function_return(decorator_symbol.module, decorator_symbol.node, False, seen)
				if returned is None:
					return None
				decorator_provider, decorator_function = returned
			else:
				decorator_provider, decorator_function = decorator_symbol.module, decorator_symbol.node
			if function_return(decorator_provider, decorator_function, True, seen) is None:
				return None
		return kind

	def function_return(provider, function, accepts_function, seen):
		if not isinstance(function, ast.FunctionDef):
			return None
		key = (provider.path, function.lineno, accepts_function)
		if key in seen or function.decorator_list:
			return None
		seen = seen | {key}
		body = function.body
		if any(
			isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for s in body for n in executable_nodes(s)
		):
			return None
		returns = [s for s in scope_statements(body) if isinstance(s, ast.Return)]
		if len(returns) != 1 or returns[0] is not body[-1] or not isinstance(returns[0].value, ast.Name):
			return None
		name = returns[0].value.id
		parameters = [*function.args.posonlyargs, *function.args.args]
		parameter = parameters[0].arg if accepts_function and parameters else None
		if any(
			isinstance(n, ast.Nonlocal) and {name, parameter}.intersection(n.names)
			for n in ast.walk(function)
		) or any(
			isinstance(n, ast.Global) and {name, parameter}.intersection(n.names)
			for n in scope_statements(body)
		):
			return None
		if resolver.dynamic_namespace(
			[runtime_body(provider.tree), body],
			{name, parameter, function.name, *callable_state},
			provider.tree,
			module=provider,
		):
			return None
		definitions = scope_bindings(body).get(name, [])
		if len(definitions) == 1 and isinstance(definitions[0], ast.FunctionDef):
			returned = definitions[0]
			if returned in body and function_descriptor(returned, provider, body, seen) == "function":
				return provider, returned
		if name != parameter:
			return None
		if not definitions:
			return provider, function
		if len(definitions) != 1 or not isinstance(definitions[0], ast.Assign) or definitions[0] not in body:
			return None
		call = definitions[0].value
		if (
			not isinstance(call, ast.Call)
			or not call.args
			or not isinstance(call.args[0], ast.Name)
			or call.args[0].id != parameter
		):
			return None
		try:
			helper = resolver.resolve_registration(reference(call.func, provider, body) or "")
		except ValueError:
			return None
		return function_return(helper.module, helper.node, True, seen)

	def inert_instance(call, class_body):
		if not isinstance(call.func, ast.Name) or call.args or call.keywords:
			return False
		if call.func.id in scope_bindings(class_body):
			return False
		binding_body, _ = namespace_binding(call.func.id, module, class_body)
		if binding_body != runtime_body(module.tree) or not namespace_safe(
			call,
			module,
			class_body,
			{
				call.func.id,
				"__class__",
				"__getattribute__",
				"__getattr__",
				"__new__",
				"__init__",
				"__setattr__",
				"__set_name__",
				"__slots__",
				*callable_state,
			},
		):
			return False
		constructor = unique_definition(module.tree, call.func.id)
		if (
			not isinstance(constructor, ast.ClassDef)
			or constructor.bases
			or constructor.decorator_list
			or constructor.keywords
			or constructor.lineno >= call.lineno
		):
			return False
		body = runtime_body(module.tree, constructor)
		bindings = scope_bindings(body)
		if {
			"__class__",
			"__getattribute__",
			"__getattr__",
			"__new__",
			"__init__",
			"__setattr__",
		}.intersection(bindings):
			return False
		for statement in body:
			targets = (
				statement.targets
				if isinstance(statement, ast.Assign)
				else [statement.target]
				if isinstance(statement, ast.AnnAssign)
				else []
			)
			if any(isinstance(t, ast.Name) and t.id == "__slots__" for t in targets):
				try:
					slots = ast.literal_eval(statement.value)
				except ValueError, TypeError:
					return False
				if not isinstance(slots, (str, tuple, list)) or "__class__" in slots:
					return False
		set_name = bindings.get("__set_name__", [])
		if set_name:
			if (
				len(set_name) != 1
				or not isinstance(set_name[0], ast.FunctionDef)
				or set_name[0].decorator_list
			):
				return False
			method = set_name[0]
			parameters = [*method.args.posonlyargs, *method.args.args]
			if len(parameters) != 3:
				return False
			self_name, *arguments = (p.arg for p in parameters)
			for statement in method.body:
				if (
					isinstance(statement, ast.Expr)
					and isinstance(statement.value, ast.Constant)
					and isinstance(statement.value.value, str)
				):
					continue
				if (
					not isinstance(statement, ast.Assign)
					or not isinstance(statement.value, ast.Name)
					or statement.value.id not in arguments
				):
					return False
				if any(
					not isinstance(t, ast.Attribute)
					or not isinstance(t.value, ast.Name)
					or t.value.id != self_name
					or t.attr.startswith("__")
					or t.attr in bindings
					for t in statement.targets
				):
					return False
		if constructor in checking:
			return False
		checking.add(constructor)
		try:
			return safe_members(constructor)
		finally:
			checking.remove(constructor)

	def safe_members(class_owner):
		properties = set()
		class_body = runtime_body(module.tree, class_owner)
		for statement in class_body:
			if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if len(statement.decorator_list) == 1 and isinstance(
					statement.decorator_list[0], ast.Attribute
				):
					decorator = statement.decorator_list[0]
					if (
						isinstance(decorator.value, ast.Name)
						and decorator.value.id == statement.name
						and statement.name in properties
						and decorator.attr in {"setter", "deleter"}
					):
						continue
				kind = function_descriptor(statement, module, class_body)
				if kind is None:
					return False
				properties.discard(statement.name)
				if any(isinstance(d, ast.Name) and d.id == "property" for d in statement.decorator_list):
					properties.add(statement.name)
			elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
				targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
				if not all(isinstance(target, ast.Name) for target in targets):
					return False
				properties.difference_update(target.id for target in targets)
				if statement.value is None:
					continue
				try:
					ast.literal_eval(statement.value)
				except ValueError, TypeError:
					if not isinstance(statement.value, ast.Call) or not inert_instance(
						statement.value, class_body
					):
						return False
			elif (
				isinstance(statement, ast.Expr)
				and isinstance(statement.value, ast.Constant)
				and isinstance(statement.value.value, str)
			):
				continue
			elif not isinstance(statement, ast.Pass):
				return False
		return True

	return safe_members(owner)
