"""Small, deterministic AST mutation operators (section 9.3 "Effectiveness").

A test suite that cannot tell the real code from a one-token change is not
evidence of the specified behavior. Operators: comparison swaps, and/or
swaps, boolean constant flips, and `not x` -> `+x` (keeps truthiness of x,
i.e. removes the negation).
"""

import ast
from dataclasses import dataclass
from typing import Iterator, List, Tuple

_SWAP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


@dataclass(frozen=True)
class Mutant:
    path: str
    lineno: int
    description: str
    source: str


def _points(tree: ast.AST) -> List[Tuple[int, str]]:
    points = []
    for index, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare) and type(node.ops[0]) in _SWAP:
            points.append((index, "compare"))
        elif isinstance(node, ast.BoolOp):
            points.append((index, "boolop"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            points.append((index, "bool"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            points.append((index, "not"))
    return points


def generate_mutants(path: str, source: str, limit: int = 50) -> Iterator[Mutant]:
    for index, kind in _points(ast.parse(source))[:limit]:
        tree = ast.parse(source)
        node = list(ast.walk(tree))[index]
        if kind == "compare":
            before = type(node.ops[0]).__name__
            node.ops[0] = _SWAP[type(node.ops[0])]()
            change = f"{before}->{type(node.ops[0]).__name__}"
        elif kind == "boolop":
            before = type(node.op).__name__
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            change = f"{before}->{type(node.op).__name__}"
        elif kind == "bool":
            change = f"{node.value}->{not node.value}"
            node.value = not node.value
        else:
            node.op = ast.UAdd()
            change = "not x->x"
        yield Mutant(path, node.lineno, f"{path}:{node.lineno} {change}", ast.unparse(tree) + "\n")
