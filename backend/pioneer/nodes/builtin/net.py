"""Сеть (spec 3.7, фаза 2; idea 003): HTTP-запрос, входящий webhook, ответ на запрос."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from pioneer.engine.node import Declaration, In, Node, NodeError, Out, Param, node
from pioneer.engine.types import jsonable

CAT, COLOR = "Сеть", "#2f6f8f"
MAX_BODY = 256 * 1024
MAX_IN_FLIGHT = 4
HOOK_PATH = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@node("builtin.http_request", title="HTTP-запрос", category=CAT, color=COLOR, doc="""
Отправляет HTTP-запрос по событию «Отправить»: GET/POST/PUT/PATCH/DELETE, тело — JSON
со входа «Тело». Ответ разбирается как JSON (иначе — текст) и выходит на «Ответ»; плюс
код статуса, «Успех» (2xx) и события «Готово» / «Ошибка».

Токены и пароли не пишите в URL и заголовки открытым текстом — используйте
`{{secret:имя}}` (страница «Секреты»): в граф попадает только ссылка.

Запрос идёт в фоне — граф не ждёт сеть; ответ приходит новой волной с тем же источником.
Одновременно — не больше 4 запросов от ноды.

Пример: Кнопка (Включение) → HTTP-запрос POST https://ntfy.sh/{{secret:topic}} → Лог.

Железо: у платы только Ethernet; без выхода в интернет внешние адреса недоступны.
""")
class HttpRequest(Node):
    method = Param("choice", "GET", title="Метод", choices=[(m, m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")],
                   doc="HTTP-метод")
    url = Param("str", "", title="URL", required=True, doc="http(s)://…; можно {{secret:имя}}")
    headers = Param("str", "", title="Заголовки", doc="По одному на строку: «Имя: значение»; можно {{secret:имя}}")
    timeout_s = Param("float", 5.0, title="Таймаут", min=0.5, max=60, unit="с", doc="Сколько ждать ответа")
    send = In("Event", title="Отправить")
    body = In("Json", None, title="Тело", doc="JSON для POST/PUT/PATCH (для GET не отправляется)")
    response = Out("Json", title="Ответ", doc="JSON ответа или текст")
    status = Out("Number", title="Статус", doc="Код HTTP; 0 — нет связи")
    ok = Out("Bool", title="Успех", doc="Статус 2xx")
    done = Out("Event", title="Готово", doc="Ответ получен; payload — ответ")
    failed = Out("Event", title="Ошибка", doc="Не удалось или статус не 2xx; payload — текст ошибки")

    def setup(self):
        self._in_flight = 0

    def on_event(self, name, ev):
        if self._in_flight >= MAX_IN_FLIGHT:
            raise NodeError(f"уже {MAX_IN_FLIGHT} запроса в пути — событие пропущено")
        url = self.secret_text(self.url).strip()
        if not url.lower().startswith(("http://", "https://")):
            raise NodeError("URL должен начинаться с http:// или https://")
        headers = {}
        for line in self.secret_text(self.headers or "").splitlines():
            if line.strip():
                k, sep, v = line.partition(":")
                if not sep:
                    raise NodeError(f"заголовок {line.strip()!r}: нужно «Имя: значение»")
                headers[k.strip()] = v.strip()
        data = None
        if self.method != "GET" and self.body is not None:
            data = json.dumps(self.body, ensure_ascii=False, default=str).encode()
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=headers, method=self.method)
        timeout = self.timeout_s
        self._in_flight += 1

        def work():
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.headers.get_content_type(), r.read(MAX_BODY)
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get_content_type() if e.headers else "", e.read(MAX_BODY)
        self.run_async(work, self._done)

    def _done(self, result, err):
        self._in_flight -= 1
        if err is not None:
            reason = getattr(err, "reason", None) or err
            self.status.set(0)
            self.ok.set(False)
            self.failed.emit(f"нет связи: {reason}")
            raise NodeError(f"нет связи: {reason}")
        code, ctype, raw = result
        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text) if "json" in ctype or text[:1] in "{[" else text
        except ValueError:
            body = text
        self.status.set(code)
        self.response.set(body)
        self.ok.set(200 <= code < 300)
        if 200 <= code < 300:
            self.done.emit(jsonable(body, 4000))
        else:
            self.failed.emit(f"HTTP {code}")


@node("builtin.webhook_in", title="Входящий webhook", category=CAT, color=COLOR, doc="""
Принимает HTTP-запрос извне: `POST https://<плата>/pioneer/api/hooks/<путь>` с телом JSON.
Тело выходит на «Данные» и событием «Получено»; выход «Запрос» — контекст для ноды «Ответ».

Защита — секретный токен: заголовок `X-Pioneer-Token: …` или `?token=…`. В параметре —
ссылка `{{secret:имя}}`. Путь уникален среди запущенных проектов.

Отвечает сервер так: если в графе сработала нода «Ответ» — её телом и статусом; иначе,
когда граф обработал запрос (с задержками и HTTP-запросами внутри), — `{"ok": true}`;
не уложился в 5 с — 504.

Пример: webhook «light» → Выбор → Цифровой выход; `curl -X POST -H 'X-Pioneer-Token: …'
-d '{"on": true}' https://плата/pioneer/api/hooks/light`.
""")
class WebhookIn(Node):
    path = Param("str", "", title="Путь", required=True, doc="Латиница, цифры, - и _: …/api/hooks/<путь>")
    token = Param("str", "", title="Токен", required=True, doc="Ссылка на секрет: {{secret:имя}}")
    data = Out("Json", title="Данные", doc="Тело запроса (JSON)")
    received = Out("Event", title="Получено", doc="Пришёл запрос; payload — тело")
    request = Out("Request", title="Запрос", doc="Контекст для ноды «Ответ»")

    def setup(self):
        if not HOOK_PATH.match(self.path or ""):
            raise NodeError("путь: латиница, цифры, - и _ (до 64 знаков)")
        if "{{secret:" not in (self.token or ""):
            raise NodeError("токен задайте ссылкой на секрет: {{secret:имя}}")
        self.secret_text(self.token)  # секрет должен существовать

    def declarations(self):
        return [Declaration("webhook", self.path or "", {"method": "POST"})]

    def expected_token(self) -> str:
        return self.secret_text(self.token)

    def deliver(self, body, ctx):
        """Вызывается в волне, начатой HTTP-обработчиком (origin remote:http)."""
        self.request.set(ctx)
        self.data.set(body)
        self.received.emit(body)


@node("builtin.reply", title="Ответ", category=CAT, color=COLOR, doc="""
Отвечает на входящий запрос (webhook, позже — команды MQTT): по событию «Ответить»
отправляет «Тело» со статусом. Провод «Запрос» можно не подключать — контекст запроса
едет вместе с волной от webhook через любые ноды (Выбор, Формула, Задержка…).

Пример: webhook «temp» → (Получено) → Ответ, Тело ← Аналоговый вход → Формула.
""")
class Reply(Node):
    status = Param("int", 200, title="Статус", min=100, max=599, doc="HTTP-код ответа")
    send = In("Event", title="Ответить")
    body = In("Json", None, title="Тело", doc="JSON ответа")
    request = In("Request", title="Запрос", doc="Не подключён — из волны")

    def on_event(self, name, ev):
        ctx = None
        if self.connected("request"):
            s = self.sample("request")
            ctx = s.value if s else None
        ctx = ctx or ev.ctx or (self.wave.ctx if self.wave else None)
        if ctx is None:
            raise NodeError("нет запроса, на который отвечать (событие пришло не от webhook)")
        if ctx.replied:
            raise NodeError("на этот запрос уже ответили")
        ctx.reply(jsonable(self.body, 100_000), self.status)


@node("builtin.json_get", title="Поле JSON", category=CAT, color=COLOR, doc="""
Достаёт значение из JSON по пути: `temp`, `sensor.value`, `items.0.name` (точка — уровень,
число — индекс списка). Пустой путь — весь JSON. Выдаёт его сразу в нескольких видах:
«Число», «Строка», «Флаг» и «Значение» (как есть). Нет такого поля — «Найдено» = false.

Пример: Входящий webhook (Данные) → Поле JSON `on` → Цифровой выход.
""")
class JsonGet(Node):
    path = Param("str", "", title="Путь", doc="Например sensor.value или items.0")
    data = In("Json", None, title="JSON")
    value = Out("Json", title="Значение")
    number = Out("Number", title="Число")
    text = Out("String", title="Строка")
    flag = Out("Bool", title="Флаг")
    found = Out("Bool", title="Найдено")

    def start(self):
        self.on_change({"data"})

    def on_change(self, changed):
        cur, ok = self.data, True
        for part in [p for p in (self.path or "").split(".") if p]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
                cur = cur[int(part)]
            else:
                cur, ok = None, False
                break
        self.found.set(ok and cur is not None)
        self.value.set(cur)
        num = None
        if isinstance(cur, (int, float)):
            num = float(cur)
        elif isinstance(cur, str):
            try:
                num = float(cur.replace(",", "."))
            except ValueError:
                num = None
        self.number.set(num if num is not None else 0.0)
        self.text.set("" if cur is None else cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False))
        self.flag.set(cur not in (None, False, 0, "", "0", "false", "off") and cur != [] and cur != {})
