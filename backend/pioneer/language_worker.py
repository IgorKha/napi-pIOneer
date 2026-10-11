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
            if "root" not in body:
                return {"items": []}
            from pioneer.compat.diagnostics import diagnose

            return {
                "items": diagnose(
                    body["code"],
                    body["root"],
                    body.get("board", {}),
                    body.get("driver_versions", {}),
                )
            }
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
    from pioneer.compat.circuitpython import paths

    project = jedi.Project(
        str(root),
        added_sys_path=[*paths(root), str(root), str(lib), str(site), str(REPO_ROOT / "backend")],
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

        def documentation(item):
            text = item.docstring()[:4000]
            name = (item.module_name or "").split(".")[0]
            if name.startswith("adafruit_"):
                chip = name.removeprefix("adafruit_")
                chip = {"character_lcd": "charlcd", "bus_device": "busdevice"}.get(chip, chip)
                text += (
                    "\n\n[Драйвер и документация на PyPI](https://pypi.org/project/adafruit-circuitpython-"
                    + chip.replace("_", "-")
                    + "/)"
                )
            return text

        return {
            "items": [
                {"label": i.description, "documentation": documentation(i)} for i in items[:5]
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
