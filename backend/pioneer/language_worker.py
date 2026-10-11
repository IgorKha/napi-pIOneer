"""Bounded Jedi analysis, never imports/executes user code in core."""

import json
import sys
from pathlib import Path
from pioneer.config import REPO_ROOT

sys.path.append(str(REPO_ROOT / "backend/pioneer/vendor_ai"))


def analyze(body):
    if body["action"] == "diagnostics":
        import ast

        try:
            ast.parse(body["code"])
            return {"items": []}
        except SyntaxError as e:
            return {
                "items": [
                    {"line": e.lineno or 1, "column": max(0, (e.offset or 1) - 1), "message": e.msg}
                ]
            }
    import jedi

    root = Path(body["root"]).resolve()
    lib = root.parent.parent / "lib"
    site = root.parent.parent / "dependencies" / f"{root.parent.name}-{root.name}"
    project = jedi.Project(
        str(root),
        added_sys_path=[str(root), str(lib), str(site), str(REPO_ROOT / "backend")],
        smart_sys_path=False,
    )
    script = jedi.Script(code=body["code"], path=str(root / body["path"]), project=project)
    line, column = body["line"], body["column"]
    action = body["action"]
    if action == "complete":
        return {
            "items": [
                {"label": i.name, "type": i.type, "detail": i.description}
                for i in script.complete(line, column)[:100]
            ]
        }
    if action == "signatures":
        return {
            "items": [
                {
                    "label": i.to_string(),
                    "parameter": i.index or 0,
                    "parameters": [p.description.removeprefix("param ") for p in i.params],
                }
                for i in script.get_signatures(line, column)[:5]
            ]
        }
    if action == "hover":
        items = script.infer(line, column)
        return {
            "items": [
                {"label": i.description, "documentation": i.docstring()[:4000]} for i in items[:5]
            ]
        }
    items = script.goto(line, column, follow_imports=True, follow_builtin_imports=False)
    result = []
    for i in items[:20]:
        if i.module_path and i.module_path.resolve().is_relative_to(root):
            result.append(
                {
                    "path": str(i.module_path.resolve().relative_to(root)),
                    "line": i.line,
                    "column": i.column,
                }
            )
    return {"items": result}


if __name__ == "__main__":
    if sys.platform == "linux":
        import os, resource, math

        resource.setrlimit(resource.RLIMIT_AS, (160 * 1024**2, 160 * 1024**2))
        os.nice(10)

    def budget():
        if sys.platform == "linux":
            used = resource.getrusage(resource.RUSAGE_SELF)
            limit = math.ceil(used.ru_utime + used.ru_stime) + 8
            resource.setrlimit(resource.RLIMIT_CPU, (limit, resource.RLIM_INFINITY))

    if "--serve" in sys.argv:
        for line in sys.stdin:
            budget()
            try:
                result = analyze(json.loads(line))
            except Exception:
                result = {"error": "analysis_failed"}
            print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        budget()
        print(json.dumps(analyze(json.load(sys.stdin)), ensure_ascii=False))
