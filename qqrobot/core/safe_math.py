"""Interpret mathematical AST nodes without Python evaluation."""
from __future__ import annotations
import ast
import math
import sympy as sp

TOOLS = {"simplify", "solve", "integrate", "diff", "limit", "series", "sum", "matrix_det", "eigenvalues"}
FUNCTIONS = {n: getattr(sp, n) for n in ("sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "exp", "log", "sqrt", "Abs", "Eq")}
FUNCTIONS["abs"] = sp.Abs
CONSTANTS = {"pi": sp.pi, "E": sp.E, "I": sp.I, "oo": sp.oo}


class Parser:
    def __init__(self, args):
        self.symbols = {}
        declarations = args.get("symbols", {})
        if not isinstance(declarations, dict) or len(declarations) > 30:
            raise ValueError("变量声明无效")
        for name, assumptions in declarations.items():
            if not isinstance(assumptions, dict) or any(
                k not in ("real", "positive", "nonnegative", "integer", "nonzero", "finite")
                or type(v) is not bool for k, v in assumptions.items()
            ):
                raise ValueError("变量假设只接受白名单布尔属性")
            self.symbols[self.name(name)] = sp.Symbol(name, **assumptions)
        if args.get("symbol"):
            self.symbol(args["symbol"])

    @staticmethod
    def name(value):
        if not isinstance(value, str) or not value.isidentifier() or value.startswith("_") or len(value) > 20:
            raise ValueError("变量名无效")
        return value

    def symbol(self, name):
        name = self.name(name)
        return self.symbols.setdefault(name, sp.Symbol(name))

    def parse(self, value):
        text = str(value if value is not None else "").strip()
        if not text or len(text) > 800:
            raise ValueError("表达式为空或过长")
        tree = ast.parse(text, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 180:
            raise ValueError("表达式太复杂")
        return self.visit(tree.body)

    def visit(self, node, depth=0):
        if depth > 25:
            raise ValueError("表达式嵌套过深")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value) or abs(node.value) > 1e100:
                raise ValueError("常数过大")
            return sp.Integer(node.value) if type(node.value) is int else sp.Rational(str(node.value))
        if isinstance(node, ast.Name):
            if node.id in self.symbols: return self.symbols[node.id]
            if node.id in CONSTANTS: return CONSTANTS[node.id]
            return self.symbol(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = self.visit(node.operand, depth + 1)
            return val if isinstance(node.op, ast.UAdd) else -val
        if isinstance(node, ast.BinOp):
            a, b = self.visit(node.left, depth + 1), self.visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if isinstance(node.op, ast.Div): return a / b
            if isinstance(node.op, ast.Pow):
                if b.is_number and (b.is_real is not True or abs(b) > 1000):
                    raise ValueError("数值幂次过大")
                return a ** b
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                node.func.id in FUNCTIONS and not node.keywords):
            if len(node.args) != (2 if node.func.id == "Eq" else 1):
                raise ValueError("函数参数数量不符")
            return FUNCTIONS[node.func.id](*(self.visit(n, depth + 1) for n in node.args))
        if isinstance(node, ast.List) and len(node.elts) <= 8:
            return [self.visit(n, depth + 1) for n in node.elts]
        raise ValueError("只允许数学运算、变量和白名单函数")


def run(call):
    tool, args = call.get("tool"), call.get("args", {})
    if tool not in TOOLS or not isinstance(args, dict): raise ValueError("工具或参数无效")
    p = Parser(args)
    if tool in ("matrix_det", "eigenvalues"):
        raw = p.parse(args.get("matrix"))
        if not isinstance(raw, list) or not raw or not all(isinstance(row, list) for row in raw):
            raise ValueError("矩阵必须是嵌套列表")
        matrix = sp.Matrix(raw)
        if matrix.rows > 8 or matrix.cols > 8 or matrix.rows != matrix.cols:
            raise ValueError("只支持不超过 8 阶的方阵")
        result = matrix.det() if tool == "matrix_det" else matrix.eigenvals()
    else:
        expr = p.parse(args.get("equation") if tool == "solve" else args.get("expr"))
        if isinstance(expr, list): raise ValueError("需要标量表达式")
        if tool == "simplify": result = sp.simplify(expr)
        else:
            var = p.symbol(args.get("symbol"))
            if tool == "solve": result = sp.solve(expr, var)
            elif tool == "diff":
                order = int(args.get("order", 1))
                if not 1 <= order <= 10: raise ValueError("导数阶数超限")
                result = sp.diff(expr, var, order)
            elif tool == "integrate":
                lo, hi = args.get("lower"), args.get("upper")
                if (lo is None) != (hi is None): raise ValueError("积分上下限必须同时给出")
                result = sp.integrate(expr, var if lo is None else (var, p.parse(lo), p.parse(hi)))
            elif tool == "sum":
                result = sp.summation(expr, (var, p.parse(args.get("lower")), p.parse(args.get("upper"))))
            elif tool == "series":
                n = int(args.get("n", 6))
                if not 1 <= n <= 20: raise ValueError("展开阶数超限")
                result = sp.series(expr, var, p.parse(args.get("point", "0")), n)
            elif tool == "limit":
                point, direction = p.parse(args.get("point")), args.get("direction", "both")
                if direction not in ("left", "right", "both"): raise ValueError("极限方向无效")
                if point in (sp.oo, -sp.oo): result = sp.limit(expr, var, point)
                elif direction != "both": result = sp.limit(expr, var, point, dir="-" if direction == "left" else "+")
                else:
                    left, right = sp.limit(expr, var, point, dir="-"), sp.limit(expr, var, point, dir="+")
                    result = left if left == right else {"left": left, "right": right,
                                                         "conclusion": "左右极限未一致，不能报告单一双侧极限"}
    text = sp.sstr(result)
    if len(text) > 5000: raise ValueError("计算结果过长")
    return text
