"""Bounded numerical plots; expressions are interpreted, never executed."""
from __future__ import annotations

import ast
import math
from pathlib import Path


def expression(text: str, variable: str, values):
    import numpy as np
    if not isinstance(text, str) or len(text) > 400:
        raise ValueError("绘图表达式过长")
    tree = ast.parse(text, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("绘图表达式太复杂")
    functions = {n: getattr(np, p) for n, p in {
        "sin": "sin", "cos": "cos", "tan": "tan", "asin": "arcsin", "acos": "arccos",
        "atan": "arctan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "exp": "exp",
        "log": "log", "sqrt": "sqrt", "abs": "abs"}.items()}

    def visit(node, depth=0):
        if depth > 20:
            raise ValueError("绘图表达式嵌套过深")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value) or abs(node.value) > 1e12:
                raise ValueError("绘图常量超限")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id == variable:
                return values
            if node.id in ("pi", "e"):
                return getattr(math, node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow):
                if np.any(np.abs(right) > 32):
                    raise ValueError("绘图幂次超过 32")
                return np.power(left, right)
            for operator, fn in ((ast.Add, np.add), (ast.Sub, np.subtract),
                                 (ast.Mult, np.multiply), (ast.Div, np.divide)):
                if isinstance(node.op, operator):
                    return fn(left, right)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                node.func.id in functions and len(node.args) == 1 and not node.keywords):
            return functions[node.func.id](visit(node.args[0], depth + 1))
        raise ValueError("绘图只允许数字、变量及白名单数学运算")

    with np.errstate(all="ignore"):
        result = np.broadcast_to(np.asarray(visit(tree.body), dtype=float), values.shape).copy()
    result[~np.isfinite(result) | (np.abs(result) > 1e12)] = np.nan
    return result


def _range(data, key, default):
    values = data.get(key, default)
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("绘图范围必须有两个端点")
    lo, hi = map(float, values)
    if not all(math.isfinite(v) and abs(v) <= 1e6 for v in (lo, hi)) or hi <= lo:
        raise ValueError("绘图范围无效")
    return lo, hi


def _number(data, key, default, lo, hi):
    value = float(data.get(key, default))
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"绘图参数 {key} 超限")
    return value


def create_plots(specs: list[dict], directory: Path) -> tuple[list[Path], list[str]]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({"text.usetex": False, "font.family": "sans-serif",
                        "font.sans-serif": ["Noto Sans CJK SC", "SimHei", "DejaVu Sans"],
                        "axes.unicode_minus": False})
    paths, warnings = [], []
    for index, spec in enumerate(specs[:2], 1):
        figure, axis = plt.subplots(figsize=(6.4, 4), dpi=140)
        try:
            kind = spec.get("kind")
            if kind in ("function", "parametric"):
                axis.set(xlabel="x", ylabel="f(x)" if kind == "function" else "y")
                variable = "x" if kind == "function" else "t"
                lo, hi = _range(spec, f"{variable}_range", [-3, 3] if variable == "x" else [0, 6.283185307])
                values = np.linspace(lo, hi, 1000)
                curves = spec.get("curves", [])
                if not isinstance(curves, list) or not 1 <= len(curves) <= 4:
                    raise ValueError("曲线数量必须为 1 至 4")
                for curve in curves:
                    x = values if kind == "function" else expression(curve["x_expression"], variable, values)
                    y = expression(curve["expression"] if kind == "function" else curve["y_expression"], variable, values)
                    # Break large sampled jumps; sampled plots are explicitly approximate.
                    finite = y[np.isfinite(y)]
                    if finite.size:
                        typical = max(1e-12, float(np.percentile(finite, 90) - np.percentile(finite, 10)))
                        jumps = np.flatnonzero(np.abs(np.diff(y)) > 8 * typical)
                        y[jumps] = np.nan
                        y[jumps + 1] = np.nan
                    axis.plot(x, y, label=str(curve.get("label", ""))[:80])
                if kind == "parametric":
                    axis.set_aspect("equal", adjustable="datalim")
            elif kind == "projectile":
                v = _number(spec, "v0", 20, 0, 1e5)
                angle = math.radians(_number(spec, "angle_deg", 45, -90, 90))
                g = _number(spec, "g", 9.8, 1e-6, 1e5)
                height = _number(spec, "initial_height", 0, 0, 1e6)
                vy = v * math.sin(angle)
                duration = (vy + math.sqrt(vy * vy + 2 * g * height)) / g
                if duration <= 0:
                    raise ValueError("抛体运动时间为空")
                t = np.linspace(0, duration, 600)
                axis.plot(v * math.cos(angle) * t, height + vy * t - .5 * g * t * t, label="trajectory")
                axis.set(xlabel="x (m)", ylabel="y (m)")
                axis.set_aspect("equal", adjustable="box")
            elif kind == "oscillator":
                amplitude = _number(spec, "amplitude", 1, 0, 1e6)
                omega = _number(spec, "omega", 2, 0, 1e5)
                phase = _number(spec, "phase", 0, -1e6, 1e6)
                damping = _number(spec, "damping", 0, 0, 1e5)
                duration = _number(spec, "duration", 10, .001, 1e5)
                if omega * duration > 600:
                    raise ValueError("振荡过密，请缩小时间范围")
                t = np.linspace(0, duration, 1000)
                axis.plot(t, amplitude * np.exp(-damping * t) * np.cos(omega * t + phase), label="x(t)")
                axis.set(xlabel="t (s)", ylabel="x (m)")
            else:
                raise ValueError("不支持的绘图模板")
            axis.set_title(str(spec.get("title", "数值示意图（非证明）"))[:100])
            for field in ("xlabel", "ylabel"):
                if spec.get(field):
                    getattr(axis, "set_" + field)(str(spec[field])[:80])
            if "y_range" in spec:
                axis.set_ylim(*_range(spec, "y_range", [-1, 1]))
            axis.grid(True, alpha=.25)
            if any(line.get_label() and not line.get_label().startswith("_") for line in axis.lines):
                axis.legend()
            figure.tight_layout()
            path = directory / f"plot-{index}.png"
            figure.savefig(path)
            paths.append(path)
        except Exception as exc:
            warnings.append(f"图 {index} 未生成：{str(exc)[:150]}")
        finally:
            plt.close(figure)
    return paths, warnings
