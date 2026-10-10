# Заметка для рабочего чата: многофайловые проекты

Контекст: [005-multifile-projects.md](005-multifile-projects.md).

## Сейчас (баг)

`pioneer/scripts.py`: worker стартует с `cwd=path.parent` = общая `data/scripts/` → `sys.path[0]` общий у всех скриптов, `import x` может подхватить чужой `x.py`. То же проверить для Python-блоков (`data/blocks/`) и Script-ноды (`data/script_cache/`). Исправить изоляцией: у каждого запуска свой корень.

## Следующим шагом (ступень А)

1. **CodeTree** — общий слой «папка с кодом» для скриптов, Python-блоков и библиотек (`data/scripts/<id>/`, `data/blocks/<slug>/`, `data/lib/<name>/`), метаданные в `pioneer.json` внутри папки. Миграция однофайловых: `<id>.py` → `<id>/main.py`, `<slug>.py` → `<slug>/block.py`.
2. **Файловое API** (общий роутер для трёх сущностей): `GET tree`, `GET/PUT file` (текст), `POST upload` / `GET download` (бинарные), `POST mkdir|move|delete`, `GET export.zip` / `POST import.zip`; безопасность путей (корень, `..`, симлинки), лимиты 5 МБ / 50 МБ.
3. **Worker:** `sys.path = [root, data/lib, backend]`, `cwd = root`, запуск `entry` или выбранного файла; трейсбеки → пути относительно корня (для кликабельных ссылок в логе).
4. **UI:** дерево файлов + вкладки в странице скриптов (и в редакторе блоков), drag-n-drop, загрузка файлов, превью картинок, «Запустить этот файл», опция «перезапускать при сохранении».

## Ступень Б (сразу после А)

CLI `pioneer push | pull | watch` поверх файлового API (дифф по sha256, удаления, опц. restart), auth — API-ключ. Вынести в `tools/pioneer-cli/` (один файл без зависимостей кроме stdlib — чтобы коллегам было просто поставить).
