"""
Arithmetic, evaluated rather than recalled.

A language model does arithmetic by predicting what the answer looks like, and
it is right often enough that the wrong ones are hard to spot. This project
already treats that as a real failure mode: `synthesis.ungrounded` exists
because "a model that misreads $61/kWh as $45/kWh writes the second as
confidently as the first", and it reports every figure in a conclusion that no
artifact supports. That check catches a bad number after the fact. This one
lets an agent avoid producing it.

The guardrail is an AST allowlist, which is a different shape from the one in
`terminal.py`. That one names permitted commands; this one names permitted
*node types*, and everything else -- every name, call, attribute, subscript,
comprehension and import -- is refused by omission rather than by pattern. A
blocklist of dangerous spellings is a losing game against a string that reaches
an evaluator: `eval` is reachable as an attribute, a lambda, a comprehension
over a class hierarchy. Refusing anything that is not literal arithmetic leaves
nothing to be clever with.

No names means no `pi` and no `sqrt`. That is deliberate: the moment this
resolves a name it needs a namespace, and a namespace is the thing that turns
an expression evaluator into an execution sandbox.
"""
import ast
import logging
import math
import operator
from typing import Any, Dict

logger = logging.getLogger("AgentHQ.Calculate")

# Binary and unary operators, mapped to the functions that implement them.
# `ast.Pow` is here but capped below: 9**9**9 is a literal arithmetic
# expression that never returns.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# An exponent above this is refused rather than evaluated. Python integers are
# arbitrary precision, so 9**9**9 does not overflow -- it allocates until the
# process dies, taking the harness with it. There is no legitimate unit
# conversion or percentage up here.
MAX_EXPONENT = 64

# Guards the same allocation from the other direction: 10**6 * 10**6 is fine,
# but repeated multiplication of large literals is not arithmetic anyone means.
MAX_MAGNITUDE = 1e100


class CalculationRefused(ValueError):
    """The expression contains something outside literal arithmetic."""


def _evaluate(node: ast.AST) -> float:
    """Walk the tree, refusing any node type not on the allowlist."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationRefused(
                f"Only numbers are allowed; got {type(node.value).__name__}.")
        return node.value

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise CalculationRefused(f"Operator {op_type.__name__} is not allowed.")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if op_type is ast.Pow and abs(right) > MAX_EXPONENT:
            raise CalculationRefused(
                f"Exponent {right} exceeds the limit of {MAX_EXPONENT}.")
        if op_type in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
            raise CalculationRefused("Division by zero.")
        result = _BIN_OPS[op_type](left, right)
        if isinstance(result, (int, float)) and abs(result) > MAX_MAGNITUDE:
            raise CalculationRefused("Result is too large to be a real quantity.")
        return result

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise CalculationRefused(f"Operator {op_type.__name__} is not allowed.")
        return _UNARY_OPS[op_type](_evaluate(node.operand))

    # Everything else. Named rather than described, because "Name is not
    # allowed" tells the agent to inline the number and try again, while a
    # generic refusal tells it to rephrase and hope.
    raise CalculationRefused(
        f"{type(node).__name__} is not allowed here. This tool evaluates literal "
        f"arithmetic only -- no variables, functions or names. Substitute the "
        f"numbers in and try again.")


async def calculate(expression: str) -> Dict[str, Any]:
    """
    Evaluate an arithmetic expression exactly.

    Never raises, for the reason every tool here documents: a throwing tool is
    retried with different wording until the step's budget is gone.
    """
    text = (expression or "").strip()
    if not text:
        return {"success": False, "refused": True, "error": "Expression is empty."}

    if len(text) > 500:
        return {"success": False, "refused": True,
                "error": "Expression is too long to be arithmetic (limit 500 chars)."}

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return {"success": False, "refused": True,
                "error": f"Not a valid expression: {exc.msg}."}

    try:
        value = _evaluate(tree)
    except CalculationRefused as exc:
        return {"success": False, "refused": True, "error": str(exc)}
    except (ArithmeticError, ValueError) as exc:
        return {"success": False, "error": f"Could not evaluate: {exc}"}

    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return {"success": False,
                "error": f"Result is not a finite number ({value})."}

    # Both spellings. The agent writes the result into an artifact, and
    # `synthesis.ungrounded` matches figures on their digits -- so handing back
    # 0.30000000000000004 where 0.3 is meant puts an ungroundable number into
    # the report that this tool exists to prevent.
    rounded = round(value, 10)
    if isinstance(rounded, float) and rounded.is_integer():
        rounded = int(rounded)

    return {
        "success": True,
        "expression": text,
        "result": rounded,
        "exact": repr(value),
    }
