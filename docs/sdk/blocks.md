# Свои блоки

Блок — своя нода графа на Python. Пишется на странице «Блоки», после сохранения появляется в палитре проектов. Код блока выполняется в отдельном процессе проекта: ошибка или зависание блока не роняет сервис и другие проекты.

## Первый блок

```python block
from pioneer.block import node, Node, In, Out, Param, Number, Bool


@node(id="hysteresis", title="Гистерезис (мой)", category="Мои блоки")
class Hysteresis(Node):
    """Включает выход, когда значение ≥ «Верхний», выключает — когда ≤ «Нижний»."""

    value = In(Number, 0, title="Значение")
    low = In(Number, 30, title="Нижний")
    high = In(Number, 35, title="Верхний")
    invert = Param(bool, False, title="Инвертировать")
    out = Out(Bool, title="Выход")

    def setup(self):
        self.state = False

    def on_change(self, changed):
        if self.value >= self.high:
            self.state = True
        elif self.value <= self.low:
            self.state = False
        self.out.set(self.state != self.invert)
```

- `@node(id=…)` — id блока; в палитре он станет `user.<id>`. `title` — заголовок ноды, `category` — раздел палитры, докстринг класса — описание в справке и тултипе.
- `In(тип, по умолчанию)` — вход. Неподключённый вход редактируется прямо на ноде.
- `Out(тип)` — выход: `self.out.set(значение)`; для `Event` — `self.out.emit(данные)`.
- `Param(bool | int | float | str | [варианты], по умолчанию)` — настройка в инспекторе, проводом не подключается.
- Типы сокетов: `Number`, `Bool`, `Event`, `String`, `Json`, а для входа «что угодно» — `Any_`.

## Обработчики

- `setup()` — при запуске проекта; здесь заводят состояние и таймеры.
- `start()` — начальные значения выходов.
- `on_change(changed)` — изменились входы (их имена в `changed`); входы читаются как `self.value`.
- `on_event(name, payload)` — пришло событие на Event-вход `name`.
- `stop()` — проект останавливается.

Каждый обработчик должен уложиться в **1 секунду**. Иначе нода краснеет, процесс блоков проекта останавливается, а «Применить» перезапускает проект. Долгое — таймерами.

## Таймеры и лог

```python block
from pioneer.block import node, Node, In, Out, Param, Bool, Event, Number


@node(id="ticker", title="Тикер", category="Мои блоки")
class Ticker(Node):
    """Событие каждые N секунд, пока «Включено»."""

    enabled = In(Bool, True, title="Включено")
    period = Param(float, 1.0, title="Период, с", min=0.05)
    tick = Out(Event, title="Тик")
    count = Out(Number, title="Счёт")

    def setup(self):
        self.n = 0
        self.every(self.period, self.fire)

    def fire(self):
        if not self.enabled:
            return
        self.n += 1
        self.count.set(self.n)
        self.tick.emit(self.n)
        if self.n % 60 == 0:
            self.log("прошла минута")
```

`self.every(сек, fn)` и `self.later(сек, fn)` вызывают функцию в своём потоке; выходы, выставленные в ней, уходят в граф новой волной. `self.log(...)` и `print(...)` пишут в лог проекта.

## Новая версия блока

Сохранение блока не трогает запущенные проекты: у них появляется «граф изменён», новый код подхватывается кнопкой «Применить». Если код с ошибкой, он всё равно сохраняется, а в палитре остаётся прошлая рабочая версия.

Железо (пины, шины) из блоков пока недоступно — для этого есть ноды «Железо» и скрипты.
