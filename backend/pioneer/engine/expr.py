"""Безопасный вычислитель выражений для ноды Expression (spec 3.7): AST, без eval.

    compile_expr("{{value}} * 0.1")(value=42) -> 4.2

Разрешено: числа, имена входов, + - * / // % **, сравнения, and/or/not, x if c else y,
функции abs min max round floor ceil sqrt sin cos tan log exp clamp, константы pi e.
"""
from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable

_FUNCS: dict[str, Callable] = {
    "abs": abs, "min": min, "max": max, "round": round, "floor": math.floor, "ceil": math.ceil,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
    "exp": math.exp, "clamp": lambda x, lo, hi: max(lo, min(hi, x)),
}
_CONSTS = {"pi": math.pi, "e": math.e, "true": True, "false": False}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: None}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
MAX_LEN = 500


class ExprError(ValueError):
    pass


def _pow(a, b):
    if abs(b) > 100 or abs(a) > 1e6:
        raise ExprError("слишком большая степень")
    return a ** b


def compile_expr(text: str, names: tuple[str, ...] = ("value", "a", "b", "c")) -> Callable[..., float]:
    """Скомпилировать выражение. `{{value}}` и `value` — одно и то же.

    Raises:
        ExprError: синтаксис или недопустимая конструкция (с понятным текстом).
    """
    src = _TEMPLATE.sub(r"\1", text.strip())
    if not src:
        raise ExprError("пустое выражение")
    if len(src) > MAX_LEN:
        raise ExprError(f"выражение длиннее {MAX_LEN} символов")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"синтаксис: {e.msg} (позиция {e.offset})") from None
    fn = _build(tree.body, set(names))

    def run(**env):
        try:
            return fn(env)
        except ZeroDivisionError:
            raise ExprError("деление на ноль") from None
        except (OverflowError, ValueError, TypeError) as e:
            raise ExprError(str(e)) from None
    return run


def _build(n: ast.AST, names: set[str]) -> Callable[[dict], object]:
    match n:
        case ast.Constant(value=v) if isinstance(v, (int, float, bool)):
            return lambda env: v
        case ast.Name(id=name):
            if name in names:
                return lambda env: env.get(name, 0.0)
            if name in _CONSTS:
                c = _CONSTS[name]
                return lambda env: c
            raise ExprError(f"неизвестное имя {name!r}; доступны: {', '.join(sorted(names))}")
        case ast.BinOp(left=l, op=op, right=r) if type(op) in _BIN:
            f = _BIN[type(op)] or _pow
            a, b = _build(l, names), _build(r, names)
            return lambda env: f(a(env), b(env))
        case ast.UnaryOp(op=ast.USub(), operand=o):
            a = _build(o, names)
            return lambda env: -a(env)
        case ast.UnaryOp(op=ast.UAdd(), operand=o):
            return _build(o, names)
        case ast.UnaryOp(op=ast.Not(), operand=o):
            a = _build(o, names)
            return lambda env: not a(env)
        case ast.BoolOp(op=op, values=vals):
            parts = [_build(v, names) for v in vals]
            if isinstance(op, ast.And):
                return lambda env: all(p(env) for p in parts)
            return lambda env: any(p(env) for p in parts)
        case ast.Compare(left=l, ops=ops, comparators=cs) if all(type(o) in _CMP for o in ops):
            first = _build(l, names)
            rest = [(_CMP[type(o)], _build(c, names)) for o, c in zip(ops, cs)]

            def cmp(env):
                x = first(env)
                for f, c in rest:
                    y = c(env)
                    if not f(x, y):
                        return False
                    x = y
                return True
            return cmp
        case ast.IfExp(test=t, body=b, orelse=o):
            tt, bb, oo = _build(t, names), _build(b, names), _build(o, names)
            return lambda env: bb(env) if tt(env) else oo(env)
        case ast.Call(func=ast.Name(id=fname), args=args, keywords=[]) if fname in _FUNCS:
            f = _FUNCS[fname]
            fargs = [_build(a, names) for a in args]
            return lambda env: f(*(a(env) for a in fargs))
        case ast.Call():
            raise ExprError(f"недопустимый вызов; функции: {', '.join(sorted(_FUNCS))}")
    raise ExprError(f"недопустимая конструкция: {type(n).__name__}")
