"""Bounded AST-to-SymPy conversion. Never eval/parse_expr user input."""
import ast
import multiprocessing
import queue

import sympy as s


FUNCTIONS = {"sin": s.sin, "cos": s.cos, "tan": s.tan, "exp": s.exp, "log": s.log,
             "ln": s.log, "sqrt": s.sqrt, "abs": s.Abs, "asin": s.asin, "acos": s.acos,
             "atan": s.atan}


def expression(text: str, assumption="real", preserve_structure=False):
    if len(text) > 500:
        raise ValueError("表达式过长")
    tree = ast.parse(text.replace("^", "**"), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("表达式过于复杂")
    symbols = {name: s.Symbol(name, **{assumption: True}) for name in ["x", "t"]}
    symbols.update({"C": s.Symbol("C", real=True), "pi": s.pi, "E": s.E, "e": s.E, "oo": s.oo})

    def visit(node, depth=0):
        if depth > 20:
            raise ValueError("嵌套过深")
        if isinstance(node, ast.Constant) and type(node.value) in [int, float]:
            if abs(node.value) > 10**8:
                raise ValueError("数值过大")
            return s.Rational(str(node.value))
        if isinstance(node, ast.Name) and node.id in symbols:
            return symbols[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth+1)
            return s.Mul(-1, value, evaluate=not preserve_structure) if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left, depth+1), visit(node.right, depth+1)
            if isinstance(node.op, ast.Add):
                return s.Add(left, right, evaluate=not preserve_structure)
            if isinstance(node.op, ast.Sub):
                return s.Add(left, s.Mul(-1, right, evaluate=not preserve_structure), evaluate=not preserve_structure)
            if isinstance(node.op, ast.Mult):
                return s.Mul(left, right, evaluate=not preserve_structure)
            if isinstance(node.op, ast.Div):
                return s.Mul(left, s.Pow(right, -1, evaluate=not preserve_structure), evaluate=not preserve_structure)
            if isinstance(node.op, ast.Pow):
                if right.is_number and (not right.is_finite or abs(right) > 100):
                    raise ValueError("指数过大")
                return s.Pow(left, right, evaluate=not preserve_structure)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FUNCTIONS and len(node.args) == 1 and not node.keywords:
                return FUNCTIONS[node.func.id](visit(node.args[0], depth+1), evaluate=not preserve_structure)
        raise ValueError("只支持 x、t、C、pi、E 及基本数学运算与函数")
    return visit(tree.body), symbols


def _calculate(spec):
    value, symbols = expression(spec["expression"], spec.get("assumption", "real"))
    x = symbols[spec.get("variable", "x")]
    op = spec["operation"]
    if op == "differentiate":
        result = s.diff(value, x)
    elif op == "integrate":
        if spec.get("lower") is not None:
            a, _ = expression(spec["lower"])
            b, _ = expression(spec["upper"])
            result = s.integrate(value, (x, a, b))
        else:
            result = s.integrate(value, x) + symbols["C"]
    else:
        if spec.get("point") is None:
            return {"status": "needs_input", "message": "请提供极限趋近点"}
        point, _ = expression(spec["point"])
        result = s.limit(value, x, point, dir=spec.get("direction", "+-"))
    if result.has(s.Integral, s.Limit, s.nan, s.zoo):
        return {"status": "pending", "message": "当前工具无法可靠求解"}
    return {"status": "computed", "expression": str(value), "result": str(result),
            "latex": s.latex(result), "operation": op, "assumption": spec.get("assumption", "real")}


def _grade(question, answer):
    kind, attrs = question["kind"], question.get("attributes", {})
    if kind == "proof":
        return {"verdict": "pending", "reason": "证明题仅供对照参考解答，不自动判分"}
    if kind == "choice":
        return {"verdict": "correct" if answer.strip().upper() == question["answer"].strip().upper() else "incorrect"}
    assumption = attrs.get("assumption", "real")
    # Keep denominators and function domains until after the domain check. Automatic
    # simplification of x/x to 1 would otherwise erase the excluded point x = 0.
    candidate, symbols = expression(answer, assumption, preserve_structure=True)
    expected, _ = expression(question["answer"], assumption, preserve_structure=True)
    if candidate.has(s.nan, s.zoo, s.oo, -s.oo) or expected.has(s.nan, s.zoo):
        return {"verdict": "pending", "reason": "含非有限值，需人工核对"}
    if kind == "numeric":
        if candidate.free_symbols or expected.free_symbols:
            return {"verdict": "pending", "reason": "本题需要数值答案"}
        correct = abs(complex(s.N(candidate - expected))) <= float(attrs.get("tolerance", 1e-6))
        return {"verdict": "correct" if correct else "incorrect"}
    x = symbols[attrs.get("variable", "x")]
    domain = {"positive": s.Interval.open(0, s.oo), "negative": s.Interval.open(-s.oo, 0),
              "nonnegative": s.Interval(0, s.oo)}.get(assumption, s.S.Reals)
    if attrs.get("domain"):
        lo, hi = attrs["domain"]
        domain = s.Interval.open(expression(str(lo))[0], expression(str(hi))[0])
    from sympy.calculus.util import continuous_domain
    try:
        # Do not equate expressions with different singularities/domains.
        c_domain = continuous_domain(candidate, x, domain)
        e_domain = continuous_domain(expected, x, domain)
        if c_domain != e_domain:
            return {"verdict": "pending", "reason": "答案定义域不同，需核对题目条件"}
    except (NotImplementedError, ValueError):
        return {"verdict": "pending", "reason": "无法确认定义域"}
    if kind == "antiderivative":
        if not candidate.has(symbols["C"]):
            return {"verdict": "incorrect", "reason": "不定积分答案需要积分常数 C"}
        delta = s.simplify(s.diff(candidate, x) - s.diff(expected, x))
    else:
        delta = s.simplify(candidate - expected)
    if delta == 0:
        return {"verdict": "correct"}
    if delta.is_zero is False or delta.is_polynomial(x):
        return {"verdict": "incorrect"}
    return {"verdict": "pending", "reason": "符号等价性无法确定，请对照参考答案"}


def _worker(out, operation, payload):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
        result = _calculate(payload) if operation == "calculate" else _grade(*payload)
        out.put(result)
    except Exception as exc:
        out.put({"status": "pending", "verdict": "pending", "reason": str(exc)[:200]})


def bounded(operation, payload, timeout=6):
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    process = ctx.Process(target=_worker, args=(out, operation, payload), daemon=True)
    process.start()
    try:
        result = out.get(timeout=timeout)
    except queue.Empty:
        result = {"status": "timeout", "verdict": "pending", "reason": "数学验证超时，待人工核对"}
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        out.close()
    return result
