"""Задел под генератор справочника (docs/ideas/004): у каждого публичного символа SDK
есть Google-style докстринг, и каждый параметр описан в `Args:`."""
import inspect
import re

import pytest

from pioneer import sdk


def _public():
    for name in sdk.__all__:
        obj = getattr(sdk, name)
        yield name, obj
        if inspect.isclass(obj) and obj.__module__ == sdk.__name__:
            for attr, member in vars(obj).items():
                if not attr.startswith("_") and (inspect.isfunction(member) or isinstance(member, property)):
                    yield f"{name}.{attr}", member


def _params(obj):
    fn = obj.__init__ if inspect.isclass(obj) else obj
    if isinstance(obj, property) or not callable(fn):
        return []
    sig = inspect.signature(fn)
    return [p for p in sig.parameters if p not in ("self", "cls")]


@pytest.mark.parametrize("name,obj", list(_public()), ids=lambda v: v if isinstance(v, str) else "")
def test_documented(name, obj):
    doc = inspect.getdoc(obj.fget if isinstance(obj, property) else obj)
    assert doc and doc.strip(), f"{name}: нет докстринга"
    if inspect.isclass(obj) and (obj.__init__ is object.__init__ or issubclass(obj, BaseException)):
        return
    params = _params(obj)
    if params == ["args", "kwargs"] and "Args:" in doc:
        return
    args = doc.split("Args:", 1)[1] if "Args:" in doc else ""
    for p in params:
        assert re.search(rf"^\s+\*{{0,2}}{p}:", args, re.M), f"{name}: параметр {p} не описан в Args:"


def test_all_is_complete():
    exported = {n for n, o in vars(sdk).items()
                if not n.startswith("_") and getattr(o, "__module__", None) == sdk.__name__
                and (inspect.isclass(o) or inspect.isfunction(o))}
    assert exported <= set(sdk.__all__), f"публичное вне __all__: {exported - set(sdk.__all__)}"
