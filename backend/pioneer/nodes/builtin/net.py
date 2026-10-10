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


# ---------------------------------------------------------------- удалённые команды (idea 003)
CMD_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
ARG = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*:\s*(Number|Bool|String|Json)\s*(?:(-?[\d.]+)\s*\.\.\s*(-?[\d.]+))?\s*$")


def parse_args_spec(spec: str) -> dict[str, dict]:
    """«angle: Number 0..180; on: Bool» → {"angle": {"type": "Number", "min": 0, "max": 180}, ...}."""
    out: dict[str, dict] = {}
    for part in [p for p in re.split(r"[;\n]", spec or "") if p.strip()]:
        m = ARG.match(part)
        if not m:
            raise NodeError(f"аргумент {part.strip()!r}: формат «имя: Number 0..180», типы Number, Bool, String, Json")
        d: dict = {"type": m[2]}
        if m[3] is not None:
            if m[2] != "Number":
                raise NodeError(f"{m[1]}: диапазон только для Number")
            d["min"], d["max"] = float(m[3]), float(m[4])
        out[m[1]] = d
    return out


def check_args(spec: dict[str, dict], args: dict) -> dict:
    """Проверить аргументы команды по схеме; ValueError с понятным текстом."""
    if not isinstance(args, dict):
        raise ValueError("args должен быть объектом")
    extra = set(args) - set(spec)
    if extra:
        raise ValueError(f"неизвестные аргументы: {', '.join(sorted(extra))}")
    out = {}
    for name, d in spec.items():
        if name not in args:
            raise ValueError(f"нет аргумента {name}")
        v = args[name]
        t = d["type"]
        if t == "Number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name}: ожидается число")
            if "min" in d and not d["min"] <= v <= d["max"]:
                raise ValueError(f"{name}: допустимо {d['min']:g}…{d['max']:g}")
            v = float(v)
        elif t == "Bool" and not isinstance(v, bool):
            raise ValueError(f"{name}: ожидается true/false")
        elif t == "String" and not isinstance(v, str):
            raise ValueError(f"{name}: ожидается строка")
        out[name] = v
    return out


@node("builtin.command", title="Команда", category=CAT, color=COLOR, doc="""
Объявляет команду API проекта, которую можно вызвать удалённо:
`POST https://<плата>/pioneer/api/remote/cmd/<имя>` с заголовком `X-Pioneer-Key` (секрет
`remote_api_key` на странице «Секреты») и телом `{"id": "1", "args": {"angle": 90}}`.

Вызвать можно только объявленные команды; аргументы проверяются по схеме до того, как
попадут в граф (`angle: Number 0..180; on: Bool; mode: String`). Ответ —
`{"id", "ok": true, "result": …}`: тело ноды «Ответ», либо просто подтверждение, когда
граф обработал команду; не уложился в 5 с — `{"ok": false, "error": "timeout"}`.

Выходы: «Пришла» (payload — аргументы), «Аргументы» (Json — дальше «Поле JSON»), «Число» и
«Флаг» — первый аргумент типа Number / Bool, «Запрос» — для «Ответа».

Пример: Команда `light.set` (`on: Bool`) → Флаг → Арбитр ← Кнопка → Цифровой выход;
Состояние `light` ← выход Арбитра.
""")
class Command(Node):
    name = Param("str", "", title="Имя", required=True, doc="латиница, цифры, . _ -: например servo.set")
    args_spec = Param("str", "", title="Аргументы", doc="«angle: Number 0..180; on: Bool»; пусто — без аргументов")
    received = Out("Event", title="Пришла", doc="payload — аргументы")
    args = Out("Json", title="Аргументы")
    number = Out("Number", title="Число", doc="Первый аргумент типа Number")
    flag = Out("Bool", title="Флаг", doc="Первый аргумент типа Bool")
    request = Out("Request", title="Запрос", doc="Контекст для ноды «Ответ»")

    def setup(self):
        if not CMD_NAME.match(self.name or ""):
            raise NodeError("имя: латиница в нижнем регистре, цифры, . _ - (до 64 знаков)")
        self.spec = parse_args_spec(self.args_spec)

    def declarations(self):
        try:
            spec = parse_args_spec(self.args_spec)
        except NodeError:
            spec = {}
        return [Declaration("command", self.name or "", {"args": spec})]

    def deliver(self, args: dict, ctx):
        self.request.set(ctx)
        self.args.set(args)
        num = next((v for k, v in args.items() if self.spec[k]["type"] == "Number"), None)
        flg = next((v for k, v in args.items() if self.spec[k]["type"] == "Bool"), None)
        if num is not None:
            self.number.set(num)
        if flg is not None:
            self.flag.set(flg)
        self.received.emit(args)


@node("builtin.state", title="Состояние", category=CAT, color=COLOR, doc="""
Публикует значение как состояние проекта: сервер читает его
`GET https://<плата>/pioneer/api/remote/state/<имя>` (ключ — как у команд), список всех —
`GET …/remote/describe`. Позже (MQTT) — retained-топик `pioneer/<плата>/state/<имя>`.

Пример: Аналоговый вход → Формула (°C) → Состояние `temp`.
""")
class State(Node):
    name = Param("str", "", title="Имя", required=True, doc="латиница, цифры, . _ -")
    value = In("Any", title="Значение")

    def setup(self):
        if not CMD_NAME.match(self.name or ""):
            raise NodeError("имя: латиница в нижнем регистре, цифры, . _ - (до 64 знаков)")

    def declarations(self):
        return [Declaration("state", self.name or "", {})]

    def start(self):
        self.on_change({"value"})

    def on_change(self, changed):
        s = self.sample("value")
        self.current = {"value": jsonable(s.value if s else self.value, 100_000),
                        "t": s.ts if s else None, "source": str(s.origin) if s else None}
        self.show(self.current)
        remote = getattr(self._rt.engine, "remote", None)
        if remote is not None:
            remote.state_changed(self.name, self.current)


@node("builtin.arbiter", title="Арбитр", category="Логика", color="#3b7a57", doc="""
Несколько источников одного значения (крутилка, команда сервера, слайдер пульта):
на выходе — значение того входа, который менялся последним. «Источник» — кто сейчас
главный (`ui:…`, `remote:http`, `gpio:J10-2`, `adc:J9-26`…).

Пример: Аналоговый вход → A, Команда servo.set (Число) → B → Арбитр → Серва: управление
у того, кто тронул последним.
""")
class Arbiter(Node):
    a = In("Number", 0.0, title="A")
    b = In("Number", 0.0, title="B")
    c = In("Number", 0.0, title="C")
    value = Out("Number", title="Значение")
    source = Out("String", title="Источник")

    def start(self):
        self.on_change({"a", "b", "c"})

    def on_change(self, changed):
        best = None
        for name in ("a", "b", "c"):
            s = self.sample(name)
            if s is not None and (best is None or s.ts > best[1].ts):
                best = (name, s)
        if best is None:
            self.value.set(self.a)
            self.source.set("")
            return
        self.value.set(getattr(self, best[0]))
        self.source.set(str(best[1].origin))


@node("builtin.publish", title="Публикация", category=CAT, color=COLOR, doc="""
Публикует событие или значение в MQTT: топик `pioneer/<плата>/event/<имя>`, тело — JSON
значения. Если вход «Когда» подключён — по событию, иначе — при каждом изменении значения.
«Сохранять» = retained: новый подписчик сразу получит последнее значение.

Брокер — страница «Настройки» → MQTT.

Пример: Цифровой вход (Включение) → Публикация `door.opened`.
""")
class Publish(Node):
    name = Param("str", "", title="Имя", required=True, doc="Последняя часть топика: латиница, цифры, . _ -")
    retain = Param("bool", False, title="Сохранять (retained)")
    when = In("Event", title="Когда")
    value = In("Any", title="Значение")

    def _bridge(self):
        b = getattr(self._rt.engine, "mqtt", None)
        if b is None:
            raise NodeError("MQTT не настроен — страница «Настройки»")
        return b

    def on_event(self, name, ev):
        v = self.value if self.connected("value") else ev.payload
        self._bridge().publish_event(self.name, jsonable(v, 100_000), self.retain)

    def on_change(self, changed):
        if not self.connected("when"):
            self._bridge().publish_event(self.name, jsonable(self.value, 100_000), self.retain)


@node("builtin.mqtt_link", title="Связь", category=CAT, color=COLOR, doc="""
Состояние связи с MQTT-брокером: «Онлайн» и события «Потеряна» / «Восстановлена».
Позволяет увести выходы в безопасное положение, если сервер управления недоступен.

Пример: Связь (Онлайн) → И ← Команда (Флаг) → Цифровой выход: без связи — выключено.
""")
class MqttLink(Node):
    online = Out("Bool", title="Онлайн")
    lost = Out("Event", title="Потеряна")
    restored = Out("Event", title="Восстановлена")

    def setup(self):
        self._b = getattr(self._rt.engine, "mqtt", None)
        if self._b is not None:
            self._b.links.append((self._rt, self))

    def start(self):
        self.online.set(bool(self._b and self._b.connected))

    def link_changed(self, on: bool):
        self.online.set(on)
        (self.restored if on else self.lost).emit()

    def stop(self):
        if getattr(self, "_b", None) is not None:
            self._b.links[:] = [(r, n) for r, n in self._b.links if n is not self]
