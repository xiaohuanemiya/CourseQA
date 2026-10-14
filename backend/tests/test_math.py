import pytest

from app.services.math_engine import _calculate, _grade, expression


@pytest.mark.parametrize("text", ["__import__('os').system('id')", "x.__class__", "[x for x in []]", "open('secret')", "2**999999", "lambda: 1"])
def test_rejects_unsafe_or_unbounded_expressions(text):
    with pytest.raises((ValueError, SyntaxError)):
        expression(text)


def test_equivalence_and_domain_are_distinct():
    assert _grade({"kind":"expression","answer":"2*x","attributes":{}}, "x+x")["verdict"] == "correct"
    assert _grade({"kind":"expression","answer":"x","attributes":{}}, "sqrt(x^2)")["verdict"] != "correct"
    assert _grade({"kind":"expression","answer":"x","attributes":{"assumption":"positive"}}, "sqrt(x^2)")["verdict"] == "correct"
    assert _grade({"kind":"expression","answer":"log(x)","attributes":{}}, "log(abs(x))")["verdict"] == "pending"


def test_antiderivative_constant_and_numeric_tolerance():
    q = {"kind":"antiderivative","answer":"x^2/2+C","attributes":{}}
    assert _grade(q,"x^2/2")["verdict"] == "incorrect"
    assert _grade(q,"(x*x)/2+C+5")["verdict"] == "correct"
    q = {"kind":"numeric","answer":"1/3","attributes":{"tolerance":.001}}
    assert _grade(q,"0.3333")["verdict"] == "correct"
    assert _grade(q,"0.3")["verdict"] == "incorrect"


def test_cancellation_does_not_erase_excluded_points():
    assert _grade({"kind":"expression","answer":"1"}, "x/x")["verdict"] == "pending"
    assert _grade({"kind":"expression","answer":"x+1"}, "(x^2-1)/(x-1)")["verdict"] == "pending"
    assert _grade({"kind":"expression","answer":"1","attributes":{"assumption":"positive"}}, "x/x")["verdict"] == "correct"
    assert _grade({"kind":"expression","answer":"x","attributes":{"assumption":"nonnegative"}}, "sqrt(x^2)")["verdict"] == "correct"


def test_calculation_missing_conditions_and_proofs():
    assert _calculate({"expression":"sin(x)/x","operation":"limit"})["status"] == "needs_input"
    assert _calculate({"expression":"sin(x)/x","operation":"limit","point":"0"})["result"] == "1"
    assert _calculate({"expression":"x^3","operation":"differentiate"})["result"] == "3*x**2"
    assert _grade({"kind":"proof","answer":"proof","attributes":{}},"anything")["verdict"] == "pending"
