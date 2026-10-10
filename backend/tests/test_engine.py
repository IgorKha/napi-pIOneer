"""Движок графов на стенде: волны, origin/контекст, inject, захваты, ноды каталога."""
import time

import pytest

from conftest import wait_for
from pioneer.engine import (
    Declaration, Engine, Graph, In, Node, NodeRegistry, Origin, Out, RequestContext, StartError, node, validate,
)
from pioneer.engine.cron import CronError, parse_cron
from pioneer.engine.expr import ExprError, compile_expr
from pioneer.engine.ops import OpError, apply_ops
from pioneer.engine.types import can_connect, coerce
from pioneer.nodes import registry

REG = registry()


@pytest.fixture
def engine(hw):
    e = Engine(hw, REG)
    yield e
    e.close()


def graph(*nodes, edges=()):
    """graph(("a", "builtin.and", {params}, {inputs}), ..., edges=[("a.out", "b.value")])"""
    g = {"nodes": [], "edges": []}
    for n in nodes:
        nid, typ, *rest = n
        params = rest[0] if rest else {}
        inputs = rest[1] if len(rest) > 1 else {}
        nt = REG.resolve(typ)
        g["nodes"].append({"id": nid, "type": nt.key, "params": {**{p.name: p.default for p in nt.params.values()},
                                                                    **params}, "inputs": inputs})
    for a, b in edges:
        g["edges"].append({"from": a.split("."), "to": b.split(".")})
    return Graph.from_json(g)


def out(engine, pid, ref):
    rt = engine.runtimes[pid]
    s = rt.sample(*ref.split("."))
    return s.value if s else None


# ---------------------------------------------------------------- типы, выражения, cron
def test_socket_types():
    assert can_connect("Bool", "Number") and can_connect("Number", "Bool") and can_connect("Number", "String")
    assert can_connect("Number", "Json") and can_connect("Bool", "Any")
    assert not can_connect("Bool", "Event") and not can_connect("Event", "Bool")
    assert not can_connect("String", "Number") and not can_connect("Event", "Any")
    assert coerce(True, "Number") == 1.0 and coerce(0.0, "Bool") is False
    assert coerce(3.0, "String") == "3" and coerce(0.25, "String") == "0.25"


def test_expression():
    assert compile_expr("{{value}} * 0.1")(value=42) == pytest.approx(4.2)
    assert compile_expr("clamp(a + b, 0, 10)")(a=7, b=8) == 10
    assert compile_expr("a if value > 0.5 else b")(value=1, a=3, b=4) == 3
    for bad, msg in [("__import__('os')", "недопустимый вызов"), ("value.real", "Attribute"),
                     ("x + 1", "неизвестное имя"), ("1 +", "синтаксис"), ("2 ** 1000", "степень")]:
        with pytest.raises(ExprError, match=msg):
            compile_expr(bad)(value=1)
    with pytest.raises(ExprError, match="деление на ноль"):
        compile_expr("1 / value")(value=0)


def test_cron():
    import datetime as dt
    c = parse_cron("*/15 8-20 * * 1-5")
    t = dt.datetime(2026, 10, 9, 20, 50)  # пятница
    assert c.next_after(t) == dt.datetime(2026, 10, 12, 8, 0)  # понедельник
    assert parse_cron("@daily").next_after(dt.datetime(2026, 1, 1, 0, 0)) == dt.datetime(2026, 1, 2)
    with pytest.raises(CronError, match="5 полей"):
        parse_cron("* *")
    with pytest.raises(CronError, match="час"):
        parse_cron("0 25 * * *")


# ---------------------------------------------------------------- операции
def test_ops_apply_validate_undo(board):
    g0 = Graph()
    g1, inv, created = apply_ops(g0, [
        {"op": "add_node", "id": "sw", "type": "builtin.ui_switch", "position": [0, 0]},
        {"op": "add_node", "id": "no", "type": "builtin.not", "position": [200, 0]},
        {"op": "connect", "from": ["sw", "value"], "to": ["no", "value"]},
        {"op": "set_param", "node": "sw", "name": "initial", "value": True},
        {"op": "move", "node": "no", "position": [300, 10]},
    ], REG, board)
    assert created == ["sw", "no"] and len(g1.edges) == 1 and g1.nodes["sw"].params["initial"] is True
    assert validate(g1, REG, board).can_start
    g2, _, _ = apply_ops(g1, inv, REG, board)  # undo
    assert g2.to_json() == g0.to_json()

    # удаление ноды и undo возвращают провода
    g3, inv3, _ = apply_ops(g1, [{"op": "remove_node", "id": "sw"}], REG, board)
    assert g3.edges == []
    g4, _, _ = apply_ops(g3, inv3, REG, board)
    assert g4.to_json()["edges"] == g1.to_json()["edges"]


def test_ops_errors(board):
    g, _, _ = apply_ops(Graph(), [
        {"op": "add_node", "id": "a", "type": "builtin.on_start"},
        {"op": "add_node", "id": "b", "type": "builtin.not"},
        {"op": "add_node", "id": "out", "type": "builtin.digital_out"},
    ], REG, board)
    with pytest.raises(OpError, match="нельзя соединить Event"):
        apply_ops(g, [{"op": "connect", "from": ["a", "started"], "to": ["b", "value"]}], REG, board)
    with pytest.raises(OpError, match="операция 2: нет ноды 'zz'"):
        apply_ops(g, [{"op": "move", "node": "b", "position": [1, 1]},
                      {"op": "move", "node": "zz", "position": [1, 1]}], REG, board)
    with pytest.raises(OpError, match="не поддерживает режим gpio_out"):
        apply_ops(g, [{"op": "set_param", "node": "out", "name": "pin", "value": "J9-26"}], REG, board)
    with pytest.raises(OpError, match="фазе 2"):
        apply_ops(g, [{"op": "group", "nodes": ["a"]}], REG, board)
    # нода без пина сохраняется, но не запускается
    rep = validate(g, REG, board)
    assert rep.ok and not rep.can_start and "Пин" in rep.text()


def test_cycle_needs_breaker(board):
    g, _, _ = apply_ops(Graph(), [
        {"op": "add_node", "id": "m", "type": "builtin.math"},
        {"op": "add_node", "id": "f", "type": "builtin.feedback"},
        {"op": "connect", "from": ["m", "result"], "to": ["f", "value"]},
        {"op": "connect", "from": ["f", "out"], "to": ["m", "a"]},
    ], REG, board)
    assert validate(g, REG, board).ok  # цикл через Обратную связь
    g2 = graph(("m", "builtin.math"), ("n", "builtin.math"), edges=[("m.result", "n.a"), ("n.result", "m.a")])
    rep = validate(g2, REG, board)
    assert not rep.ok and "цикл" in rep.text()


def test_connect_replaces_existing(board):
    g, _, _ = apply_ops(Graph(), [
        {"op": "add_node", "id": "a", "type": "builtin.ui_switch"},
        {"op": "add_node", "id": "b", "type": "builtin.ui_switch"},
        {"op": "add_node", "id": "n", "type": "builtin.not"},
        {"op": "connect", "from": ["a", "value"], "to": ["n", "value"]},
    ], REG, board)
    g2, inv, _ = apply_ops(g, [{"op": "connect", "from": ["b", "value"], "to": ["n", "value"]}], REG, board)
    assert [e.src for e in g2.edges] == [("b", "value")]
    assert apply_ops(g2, inv, REG, board)[0].to_json()["edges"] == g.to_json()["edges"]


# ---------------------------------------------------------------- волны
def test_wave_is_glitch_free(engine):
    """Ромб: sw → NOT → AND(sw, NOT sw). Без транзакций AND на миг видел бы (true, true)."""
    seen = []

    class Spy:
        pass
    g = graph(("sw", "builtin.ui_switch"), ("no", "builtin.not"), ("and", "builtin.and"), ("w", "builtin.watch"),
              edges=[("sw.value", "no.value"), ("sw.value", "and.a"), ("no.out", "and.b"), ("and.out", "w.value")])
    engine.on_wave_done(lambda rt, w: seen.append((str(w.origin), list(w.nodes_run))))
    engine.start_project("p", "Ромб", g)
    for v in (True, False, True):
        engine.ui("p", "sw", v)
    assert out(engine, "p", "and.out") is False
    ui_waves = [nodes for o, nodes in seen if o.startswith("ui")]
    assert all(n.count("and") == 1 for n in ui_waves)  # AND пересчитан один раз за волну
    assert all(n.index("no") < n.index("and") for n in ui_waves)


def test_origin_and_request_context(engine):
    g = graph(("sw", "builtin.ui_switch"), ("d", "builtin.delay", {"mode": "both"}, {"delay_ms": 30}),
              ("w", "builtin.watch"), edges=[("sw.value", "d.value"), ("d.out", "w.value")])
    engine.start_project("p", "Ctx", g)
    rt = engine.runtimes["p"]
    done = []
    ctx = RequestContext(transport="test", on_complete=[lambda c: done.append(time.monotonic())])
    t0 = time.monotonic()
    engine.inject("p", Origin("remote", "test"), {("sw", "value"): True}, ctx=ctx)
    assert wait_for(lambda: done, 1)
    assert done[0] - t0 >= 0.025  # запрос завершился только после продолжения через Delay
    s = rt.sample("d", "out")
    assert s.value is True and str(s.origin) == "remote:test"  # продолжение унаследовало origin
    assert rt.sample("sw", "value").origin.kind == "remote"


def test_request_timeout(engine):
    g = graph(("sw", "builtin.ui_switch"), ("d", "builtin.delay", {"mode": "both"}, {"delay_ms": 300}),
              edges=[("sw.value", "d.value")])
    engine.start_project("p", "Ctx", g)
    timeouts = []
    ctx = RequestContext(deadline=time.monotonic() + 0.05, on_timeout=[lambda c: timeouts.append(c.id)])
    engine.inject("p", Origin("remote", "test"), {("sw", "value"): True}, ctx=ctx)
    assert wait_for(lambda: timeouts == [ctx.id], 1)
    assert wait_for(lambda: ctx.done, 1)  # но продолжение всё же доехало


def test_node_error_isolated(engine):
    g = graph(("a", "builtin.ui_number"), ("m", "builtin.math", {"op": "div"}, {"a": 1}),
              ("ok", "builtin.not"), edges=[("a.value", "m.b")])
    lines = []
    engine.on_log = lambda pid, line: lines.append(line)
    engine.start_project("p", "Ошибки", g)
    rt = engine.runtimes["p"]
    assert rt.node_state("m")["state"] == "error" and "деление на ноль" in rt.node_state("m")["error"]
    engine.ui("p", "a", 4)
    assert out(engine, "p", "m.result") == 0.25 and out(engine, "p", "ok.out") is True
    assert any(line["level"] == "error" and line["node"] == "m" for line in lines)


def test_inject_requires_running(engine):
    with pytest.raises(Exception, match="не запущен"):
        engine.ui("nope", "x", 1)


# ---------------------------------------------------------------- железо (сценарии из «Готово, когда»)
def test_button_toggle_led(engine, bench):
    """Мокап 1: кнопка → toggle → LED."""
    bench.add("button", "btn", pin="J10-2", bounce_ms=3)
    led = bench.add("led", pin="J9-7")
    g = graph(("in", "builtin.digital_in", {"pin": "J10-2", "debounce_ms": 10}),
              ("t", "builtin.toggle"), ("o", "builtin.digital_out", {"pin": "J9-7"}),
              edges=[("in.rising", "t.toggle"), ("t.out", "o.value")])
    engine.start_project("p", "Свет", g)
    assert led.state()["on"] is False
    for expect in (True, False):
        bench.action("btn", "click", {"ms": 60})
        assert wait_for(lambda: led.state()["on"] is expect, 1)
        assert wait_for(lambda: out(engine, "p", "in.value") is False, 1)  # отпущена
    rt = engine.runtimes["p"]
    assert str(rt.sample("t", "out").origin) == "gpio:J10-2"
    engine.stop_project("p")
    assert bench.level("J9-7") is False and "J9-7" not in engine.hw.res.claims


def test_adc_hysteresis_delayed_relay(engine, bench):
    """Мокап 3: ADC → гистерезис → реле с задержкой включения."""
    src = bench.add("voltage", "pot", pin="J9-26", volts=0.2)
    g = graph(("adc", "builtin.analog_in", {"pin": "J9-26", "interval_ms": 10, "average": 1}),
              ("h", "builtin.hysteresis", {}, {"low": 0.8, "high": 1.2}),
              ("d", "builtin.delay", {"mode": "on"}, {"delay_ms": 80}),
              ("r", "builtin.digital_out", {"pin": "J9-16"}),
              edges=[("adc.volts", "h.value"), ("h.out", "d.value"), ("d.out", "r.value")])
    engine.start_project("p", "Реле", g)
    bench.action("pot", "set", {"volts": 1.5})
    assert wait_for(lambda: out(engine, "p", "h.out") is True, 1)
    assert bench.level("J9-16") is False  # ещё ждём задержку
    assert wait_for(lambda: bench.level("J9-16") is True, 1)
    bench.action("pot", "set", {"volts": 1.0})  # между порогами — держит
    time.sleep(0.05)
    assert bench.level("J9-16") is True
    bench.action("pot", "set", {"volts": 0.5})
    assert wait_for(lambda: bench.level("J9-16") is False, 1)  # выключение — сразу
    assert src


def test_adc_formula_uart(engine, bench):
    """Мокап 2: ADC → формула → UART (петля на стенде)."""
    bench.add("voltage", pin="J10-3", volts=0.9)
    bench.add("uart_loopback", device="/dev/ttyS3")
    g = graph(("adc", "builtin.analog_in", {"pin": "J10-3", "interval_ms": 20, "average": 1}),
              ("f", "builtin.expression", {"expr": "round({{value}} * 100 / 1.8)"}),
              ("tick", "builtin.interval", {}, {"period_ms": 30}),
              ("tx", "builtin.uart_write", {"interface": "uart3"}),
              ("rx", "builtin.uart_read", {"interface": "uart3"}),
              edges=[("adc.volts", "f.value"), ("f.result", "tx.text"), ("tick.tick", "tx.send")])
    engine.start_project("p", "Телеметрия", g)
    assert wait_for(lambda: out(engine, "p", "rx.line") == "50", 2)


def test_two_projects_and_output_conflict(engine, bench):
    a = graph(("o", "builtin.digital_out", {"pin": "J9-7"}, {"value": True}))
    b = graph(("o", "builtin.digital_out", {"pin": "J9-12"}, {"value": True}),
              ("i", "builtin.digital_in", {"pin": "J10-2"}))
    c = graph(("i", "builtin.digital_in", {"pin": "J10-2"}),
              ("o", "builtin.digital_out", {"pin": "J9-7"}))
    engine.start_project("a", "Полив", a)
    engine.start_project("b", "Свет", b)  # другие пины + общий вход
    assert bench.level("J9-7") and bench.level("J9-12")
    with pytest.raises(StartError, match='J9-7 занят.*проект «Полив»'):
        engine.start_project("c", "Конфликт", c)
    assert "c" not in engine.runtimes
    assert set(engine.hw.res.claims["J10-2"].owners) == {"project:b"}  # откат захватов c


def test_safe_state_and_invert(engine, bench):
    g = graph(("o", "builtin.digital_out", {"pin": "J9-7", "invert": True, "safe_state": "off"}, {"value": True}))
    engine.start_project("p", "Реле LOW", g)
    assert bench.level("J9-7") is False  # value true + invert = LOW
    engine.stop_project("p")
    assert bench.level("J9-7") is False  # но safe=off + invert = HIGH — освобождённый пин тянется подтяжкой


def test_ui_slider_to_pwm(engine, bench):
    led = bench.add("led", pin="J9-12")
    g = graph(("s", "builtin.ui_slider", {"step": 0.1}), ("pwm", "builtin.soft_pwm", {"pin": "J9-12"}),
              edges=[("s.value", "pwm.duty")])
    engine.start_project("p", "Яркость", g)
    engine.ui("p", "s", 0.42)
    assert out(engine, "p", "s.value") == pytest.approx(0.4)
    assert led.state()["brightness"] == pytest.approx(0.4)
    assert engine.runtimes["p"].views["s"] == {"value": pytest.approx(0.4)}


def test_pulse_counter_latch(engine):
    g = graph(("b", "builtin.ui_button"), ("p", "builtin.pulse", {}, {"duration_ms": 40}),
              ("c", "builtin.counter"), ("l", "builtin.latch"),
              edges=[("b.click", "p.trigger"), ("b.click", "c.up"), ("b.click", "l.set_on")])
    engine.start_project("x", "Импульс", g)
    engine.ui("x", "b", "click")
    assert out(engine, "x", "p.out") is True and out(engine, "x", "c.count") == 1 and out(engine, "x", "l.out")
    assert wait_for(lambda: out(engine, "x", "p.out") is False, 1)


def test_manifest_names_unique(hw):
    reg = NodeRegistry()

    @node("test.command", title="Команда", category="Тест", doc="Тестовая нода с объявлением.", registry=reg)
    class Cmd(Node):
        name = In("String", "")
        fired = Out("Event")

        def declarations(self):
            return [Declaration("command", self._rt.graph.nodes[self.id].inputs.get("name", "x"))]

    e = Engine(hw, reg)
    try:
        g = Graph.from_json({"nodes": [{"id": "c", "type": "test.command@1", "inputs": {"name": "light"}}]})
        e.start_project("a", "Свет", g)
        assert [d.name for d in e.runtimes["a"].manifest()] == ["light"]
        with pytest.raises(StartError, match="command «light» уже объявлен проектом «Свет»"):
            e.start_project("b", "Свет 2", g)
    finally:
        e.close()


def test_builtin_catalog_documented():
    for nt in REG.types.values():
        assert len(nt.doc) > 40, f"{nt.key}: нет doc"
        for p in nt.params.values():
            assert p.title, f"{nt.key}.{p.name}: нет подписи"


def test_board_view_ops(board):
    g, inv, _ = apply_ops(Graph(), [{"op": "move", "node": "@board", "position": [-500, 20]},
                                    {"op": "collapse", "node": "@board", "collapsed": True}], REG, board)
    assert g.view["board"] == {"position": [-500.0, 20.0], "collapsed": True}
    assert Graph.from_json(g.to_json()).view == g.view
    g2, _, _ = apply_ops(g, inv, REG, board)
    assert g2.view["board"] == {"position": [-420.0, 0.0], "collapsed": False}
