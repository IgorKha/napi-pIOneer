"""Small runnable examples with no GPIO/network side effects or autostart."""


def make(ident, title, description, nodes, edges):
    return {
        "id": ident,
        "name": title,
        "description": description,
        "graph": {
            "nodes": [
                {
                    "id": n[0],
                    "type": "builtin." + n[1] + "@1",
                    "position": [i * 230, 0],
                    "params": n[2] if len(n) > 2 else {},
                    "inputs": n[3] if len(n) > 3 else {},
                }
                for i, n in enumerate(nodes)
            ],
            "view": {"board": {"position": [-300, 0], "collapsed": True}},
            "edges": [{"from": a.split("."), "to": b.split(".")} for a, b in edges],
        },
    }


TEMPLATES = [
    make(
        "pulse",
        "Кнопка → импульс 10 секунд",
        "Кнопка пульта запускает импульс; Watch показывает состояние. Пины не используются.",
        [
            ("button", "ui_button"),
            ("pulse", "pulse", {}, {"duration_ms": 10000}),
            ("watch", "watch"),
        ],
        [("button.click", "pulse.trigger"), ("pulse.out", "watch.value")],
    ),
    make(
        "counter",
        "Кнопка → счётчик",
        "Кнопка пульта увеличивает счётчик. Для сохранения между запусками включите «Сохранять состояние».",
        [("button", "ui_button"), ("counter", "counter"), ("watch", "watch")],
        [("button.click", "counter.up"), ("counter.count", "watch.value")],
    ),
    make(
        "hysteresis",
        "Температура → гистерезис",
        "Слайдер имитирует температуру. Порог включения 35, выключения 30. Watch показывает результат.",
        [
            ("temperature", "ui_slider", {"min": 0, "max": 60, "initial": 25}),
            ("threshold", "hysteresis"),
            ("watch", "watch"),
        ],
        [("temperature.value", "threshold.value"), ("threshold.out", "watch.value")],
    ),
]
