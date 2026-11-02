"""Restricted offline interpreter for the checked-in YAML, not a Google emulator.

Only the syntax and stdlib functions actually used by this definition are accepted.
Cloud effects are injected. Unsupported expressions/nodes fail, never silently skip.
"""

import ast
import base64
import copy
import hashlib
import json
import re
from urllib.parse import quote


class WorkflowError(Exception):
    def __init__(self, value):
        self.value = value
        super().__init__(str(value))


class Returned(BaseException):
    def __init__(self, value):
        self.value = value


class Jump(BaseException):
    def __init__(self, label):
        self.label = label


def get_type(value):
    return {
        dict: "map",
        list: "list",
        str: "string",
        bytes: "bytes",
        type(None): "null",
        bool: "boolean",
        int: "integer",
        float: "double",
    }[type(value)]


class Engine:
    def __init__(self, definition, effects, execution_id="execution-001"):
        self.definition, self.effects, self.execution_id = definition, effects, execution_id
        self.now, self.logs, self.calls = 0, [], 0
        self.functions = {
            "map.get": lambda m, k: m.get(k),
            "map.delete": lambda m, k: {n: v for n, v in m.items() if n != k},
            "map.merge": lambda a, b: {**a, **b},
            "list.concat": lambda xs, x: [*xs, x],
            "text.substring": lambda s, low, high: s[low:high],
            # RE2's final $ requires end-of-text, unlike Python's before-newline $.
            "text.match_regex": lambda s, r: (
                re.search(r[:-1] + r"\Z" if r.endswith("$") else r, s) is not None
            ),
            "text.url_encode": lambda s: quote(s, safe=""),
            "text.encode": lambda s: s.encode(),
            "base64.encode": lambda b: base64.b64encode(b).decode(),
            "hash.compute_checksum": lambda b, algo: hashlib.new(algo.lower(), b).digest(),
            "json.decode": json.loads,
            "sys.get_env": lambda key: self.execution_id,
            "sys.now": lambda: self.now,
            "len": len,
            "int": int,
            "keys": lambda m: sorted(m),
            "get_type": get_type,
            "default": lambda x, fallback: fallback if x is None else x,
            "if": lambda condition, yes, no: yes if condition else no,
        }

    def value(self, value, env):
        if isinstance(value, dict):
            return {k: self.value(v, env) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, env) for v in value]
        if not isinstance(value, str) or not value.startswith("${"):
            return copy.deepcopy(value)
        assert value.endswith("}")
        expression = value[2:-1].replace("if(", "choose(")
        return self.expression(ast.parse(expression, mode="eval").body, env)

    def expression(self, node, env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return (
                {"true": True, "false": False, "null": None}.get(node.id, env.get(node.id))
                if node.id in {"true", "false", "null"}
                else env[node.id]
            )
        if isinstance(node, ast.Attribute):
            return self.expression(node.value, env)[node.attr]
        if isinstance(node, ast.Subscript):
            assert not isinstance(node.slice, ast.Slice), "Workflows has no Python slicing"
            return self.expression(node.value, env)[self.expression(node.slice, env)]
        if isinstance(node, ast.List):
            return [self.expression(n, env) for n in node.elts]
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).replace("choose", "if")
            assert name in self.functions, name
            return self.functions[name](*(self.expression(n, env) for n in node.args))
        if isinstance(node, ast.UnaryOp):
            val = self.expression(node.operand, env)
            return not val if isinstance(node.op, ast.Not) else -val
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(self.expression(v, env) for v in node.values)
            return any(self.expression(v, env) for v in node.values)
        if isinstance(node, ast.BinOp):
            a, b = self.expression(node.left, env), self.expression(node.right, env)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            assert isinstance(node.op, ast.Mult)
            return a * b
        if isinstance(node, ast.Compare):
            assert len(node.ops) == 1
            a, b = self.expression(node.left, env), self.expression(node.comparators[0], env)
            op = node.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                numeric = type(a) in (int, float) and type(b) in (int, float)
                if a is not None and b is not None and type(a) is not type(b) and not numeric:
                    raise WorkflowError(
                        {
                            "tags": ["TypeError"],
                            "message": "incompatible operand types for comparison: "
                            + type(a).__name__
                            + " and "
                            + type(b).__name__,
                        }
                    )
            if isinstance(op, ast.Eq):
                return a == b
            if isinstance(op, ast.NotEq):
                return a != b
            if isinstance(op, ast.In):
                return a in b
            if isinstance(op, ast.Lt):
                return a < b
            if isinstance(op, ast.Gt):
                return a > b
            if isinstance(op, ast.GtE):
                return a >= b
        raise AssertionError("Unsupported workflow expression: " + ast.dump(node))

    def run(self, name, **args):
        function = self.definition[name]
        assert set(args) == set(function["params"]), (name, args)
        try:
            self.steps(function["steps"], args)
        except Returned as result:
            return result.value
        return None

    def steps(self, steps, env):
        labels = [next(iter(s)) for s in steps]
        assert len(labels) == len(set(labels)), "Duplicate step names"
        index = 0
        while index < len(steps):
            self.calls += 1
            assert self.calls < 100000, "Unbounded workflow loop"
            node = steps[index][labels[index]]
            try:
                self.step(node, env)
                if "next" in node:
                    raise Jump(node["next"])
                index += 1
            except Jump as jump:
                if jump.label not in labels:
                    raise
                index = labels.index(jump.label)

    def step(self, node, env):
        if "try" in node:
            count = 0
            while True:
                try:
                    body = node["try"]
                    self.steps(body["steps"], env) if "steps" in body else self.step(body, env)
                    break
                except WorkflowError as error:
                    if (
                        "retry" in node
                        and count < node["retry"]["max_retries"]
                        and (
                            error.value.get("code") in {429, 502, 503, 504}
                            or "ConnectionError" in error.value.get("tags", [])
                            or "TimeoutError" in error.value.get("tags", [])
                        )
                    ):
                        self.now += min(8, 2**count)
                        count += 1
                        continue
                    if "except" not in node:
                        raise
                    env[node["except"]["as"]] = error.value
                    self.steps(node["except"]["steps"], env)
                    break
        elif "assign" in node:
            for assignment in node["assign"]:
                assert len(assignment) == 1
                key, value = next(iter(assignment.items()))
                target = ast.parse(key, mode="eval").body
                evaluated = self.value(value, env)
                if isinstance(target, ast.Name):
                    env[key] = evaluated
                else:
                    assert isinstance(target, ast.Subscript)
                    self.expression(target.value, env)[self.expression(target.slice, env)] = (
                        evaluated
                    )
        elif "call" in node:
            args = self.value(node.get("args", {}), env)
            call = node["call"]
            if call == "sys.log":
                self.logs.append(copy.deepcopy(args))
                result = None
            elif call == "sys.sleep":
                self.now += args["seconds"]
                result = None
            elif call in {"http.get", "http.post"}:
                assert args["auth"] == {"type": "OAuth2"}
                assert 0 < args["timeout"] <= 60
                result = self.effects(call, args)
            else:
                result = self.run(call, **args)
            if "result" in node:
                env[node["result"]] = result
        elif "switch" in node:
            for case in node["switch"]:
                if self.value(case["condition"], env):
                    if "steps" in case:
                        self.steps(case["steps"], env)
                    self.step(
                        {k: v for k, v in case.items() if k not in {"condition", "steps"}}, env
                    )
                    break
        elif "for" in node:
            loop = node["for"]
            if "in" in loop:
                values = self.value(loop["in"], env)
            else:
                low, high = self.value(loop["range"], env)
                values = range(low, high + 1)
            original = set(env)
            for value in values:
                local = {**env, loop["value"]: value}
                try:
                    self.steps(loop["steps"], local)
                except Jump as jump:
                    if jump.label != "continue":
                        raise
                for key in original:
                    env[key] = local[key]
        elif "return" in node:
            raise Returned(self.value(node["return"], env))
        elif "raise" in node:
            raise WorkflowError(self.value(node["raise"], env))
        elif "next" in node:
            raise Jump(node["next"])
        else:
            assert node == {}, node
