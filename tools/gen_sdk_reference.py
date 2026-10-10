"""Сгенерировать справочник SDK в JSON (для статического сайта/CI, docs/ideas/004).

    make reference                       # -> sdk-reference.json
    cd backend && uv run python ../tools/gen_sdk_reference.py [out.json]

Плата — описание из boards/napi-c.yaml с дефолтным uEnv (без живого DT).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pioneer.board import load_board  # noqa: E402
from pioneer.config import Config
from pioneer.help import build_reference
from pioneer.nodes import registry


def main() -> None:
    cfg = Config()
    board = load_board(cfg.board_file, list(cfg.default_overlays))
    ref = build_reference(board, registry())
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sdk-reference.json")
    out.write_text(json.dumps(ref, ensure_ascii=False, indent=1))
    print(f"{out}: {len(ref['sdk'])} символов SDK, {len(ref['guides'])} руководств, {len(ref['nodes'])} нод")


if __name__ == "__main__":
    main()
