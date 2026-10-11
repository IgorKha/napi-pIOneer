"""Универсальные изображения; ESP32-CAM и MJPEG вынесены в отдельную задачу."""

import urllib.request

from pioneer.engine.node import In, Node, NodeError, Out, Param, node
from pioneer.images import MAX_IMAGE, image_type
from pioneer.nodes.builtin.net import read_body

CAT, COLOR = "Изображения", "#745da0"


@node(
    "builtin.image_url",
    title="Изображение по URL",
    category=CAT,
    color=COLOR,
    doc="""
По событию «Получить» скачивает PNG/JPEG с обычного HTTP(S) URL и выдаёт Image.
Подходит для тестового сервера или любого endpoint со снимком; поток MJPEG не читает.
URL поддерживает {{secret:имя}}. Максимум 2 МБ, один запрос одновременно, таймаут
0.5–60 с. Автоматических повторов нет: следующий импульс запускает новый запрос.
«Готово» срабатывает после установки изображения; превью появляется прямо на ноде.
При ошибке прежнее изображение очищается, событие «Готово» не выдаётся.

Пример: Кнопка (пульт) → Получить; Изображение и Готово → HTTP: обработка.
""",
)
class ImageUrl(Node):
    url = Param(
        "str", "", title="URL", required=True, doc="http(s)://…/image.png; можно {{secret:имя}}"
    )
    timeout_s = Param("float", 5.0, title="Таймаут", min=0.5, max=60, unit="с")
    fetch = In("Event", title="Получить")
    image = Out("Image", title="Изображение")
    ready = Out("Event", title="Готово")
    failed = Out("Event", title="Ошибка")

    def setup(self):
        self._busy = False

    def on_event(self, name, ev):
        if self._busy:
            raise NodeError("загрузка изображения уже выполняется")
        url = self.secret_text(self.url).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise NodeError("URL должен начинаться с http:// или https://")
        timeout = self.timeout_s
        self._busy = True

        def work():
            with urllib.request.urlopen(url, timeout=timeout) as r:
                raw = read_body(r, MAX_IMAGE)
                image_type(raw)
                return raw

        self.run_async(work, self._done)

    def _done(self, raw, err):
        self._busy = False
        if err is not None:
            self.image.set(None)
            # URL может содержать секрет — не выводим исключение urllib целиком.
            message = (
                str(err)
                if isinstance(err, NodeError)
                else f"не удалось загрузить изображение ({type(err).__name__})"
            )
            self.failed.emit(message)
            raise NodeError(message)
        self.image.set(raw)
        self.ready.emit()


@node(
    "builtin.save_image",
    title="Сохранить изображение",
    category=CAT,
    color=COLOR,
    doc="""
По событию «Сохранить» записывает PNG/JPEG со входа Image в data/images/<проект>/.
Имена уникальны, файлы не перезаписываются; максимум 100 снимков / 50 МБ на проект.
При заполнении — ошибка, автоматического удаления нет. Очистка пока через SSH.
«Файл» — относительный путь внутри data; «Готово» выдаётся после записи.
Запись идёт в фоне, один файл одновременно. Снимки сохраняются после перезапуска.
""",
)
class SaveImage(Node):
    save = In("Event", title="Сохранить")
    image = In("Image", title="Изображение")
    file = Out("String", title="Файл")
    done = Out("Event", title="Готово")
    failed = Out("Event", title="Ошибка")

    def setup(self):
        self._busy = False
        if getattr(self._rt.engine, "images", None) is None:
            raise NodeError("хранилище изображений не подключено")

    def on_event(self, name, ev):
        if self._busy:
            raise NodeError("запись изображения уже выполняется")
        raw = self.image
        image_type(raw)
        store, pid = self._rt.engine.images, self._rt.project_id
        self._busy = True
        self.run_async(lambda: store.save(pid, raw), self._done)

    def _done(self, name, err):
        self._busy = False
        if err is not None:
            message = str(err) if isinstance(err, NodeError) else "не удалось сохранить изображение"
            self.failed.emit(message)
            raise NodeError(message)
        pid = self._rt.project_id
        path = f"images/{pid}/{name}"
        self.file.set(path)
        self.show({"download": f"/pioneer/api/projects/{pid}/images/{name}", "file": path})
        self.done.emit(path)
