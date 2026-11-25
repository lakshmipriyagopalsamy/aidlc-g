import ast

from aidlc_g.mutation import generate_mutants

SOURCE = '''def f(a, b, flag=True):
    if a == b and not flag:
        return a in (1, 2)
    return a < b or False
'''


def test_every_operator_fires_once_per_site():
    kinds = sorted(m.description.split(" ", 1)[1] for m in generate_mutants("src/f.py", SOURCE))
    assert kinds == sorted(["Eq->NotEq", "In->NotIn", "Lt->GtE", "And->Or", "Or->And",
                            "True->False", "False->True", "not x->x"])


def test_mutants_are_valid_and_distinct():
    mutants = list(generate_mutants("src/f.py", SOURCE))
    assert len({m.source for m in mutants}) == len(mutants)
    for m in mutants:
        ast.parse(m.source)
        assert m.source != SOURCE


def test_limit_and_location():
    mutants = list(generate_mutants("src/f.py", SOURCE, limit=2))
    assert len(mutants) == 2 and all(m.description.startswith("src/f.py:") for m in mutants)


def test_docstrings_and_strings_are_not_mutated():
    assert list(generate_mutants("src/g.py", '"""doc"""\nX = "True"\n')) == []
