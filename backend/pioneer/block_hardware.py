"""Hardware RPC for block workers, scoped to the project's ResourceManager owner."""


class Device:
    def __init__(self, client, handle):
        self.client, self.handle = client, handle

    def write(self, data):
        self.client.call("i2c", h=self.handle, kind="write", data=bytes(data).hex())

    def read(self, n):
        return bytes.fromhex(self.client.call("i2c", h=self.handle, kind="read", n=n))

    def write_read(self, data, n):
        return bytes.fromhex(
            self.client.call("i2c", h=self.handle, kind="write_read", data=bytes(data).hex(), n=n)
        )

    def read_reg(self, reg, n=1):
        return self.write_read(bytes([reg]), n)

    def write_reg(self, reg, data):
        self.write(bytes([reg]) + bytes(data))


class BlockHW:
    def __init__(self, node):
        self.node = node

    def call(self, op, **args):
        return self.node._host.hardware(self.node.id, op, args)

    def i2c(self, interface, addr):
        return Device(self, self.call("i2c_open", interface=interface, addr=addr))

    def output(self, pin, initial=False):
        self.call("acquire", pin=pin, mode="gpio_out", initial=initial)

    def input(self, pin):
        self.call("acquire", pin=pin, mode="gpio_in")

    def write(self, pin, value):
        self.call("write", pin=pin, value=bool(value))

    def read(self, pin):
        return self.call("read", pin=pin)


class HardwareRPC:
    def __init__(self, rt):
        self.rt, self.handles = rt, {}

    def call(self, nid, op, a):
        from pioneer.userblocks import BlockProxy

        if not isinstance(self.rt.nodes.get(nid), BlockProxy):
            raise ValueError("нет активной ноды блока")
        hw = self.rt.hw.hw
        owner, name = self.rt.hw.owner, self.rt.hw.owner_name
        if op == "i2c_open":
            handle = len(self.handles) + 1
            self.handles[(nid, handle)] = hw.i2c(owner, name, a["interface"], int(a["addr"]))
            return handle
        if op == "i2c":
            dev = self.handles[(nid, a["h"])]
            kind = a["kind"]
            data = bytes.fromhex(a.get("data", ""))
            if len(data) > 4096:
                raise ValueError("I2C до 4096 байт")
            n = int(a.get("n", 0))
            if not 0 <= n <= 4096:
                raise ValueError("I2C read до 4096 байт")
            if kind == "write":
                dev.write(data)
                return None
            if kind == "read":
                return dev.read(n).hex()
            if kind == "write_read":
                return dev.write_read(data, n).hex()
            raise ValueError("неверная операция I2C")
        if op == "acquire":
            if a["mode"] not in ("gpio_in", "gpio_out"):
                raise ValueError("неверный режим пина")
            hw.acquire(owner, name, a["pin"], a["mode"], initial=a.get("initial", False))
            return None
        if a.get("pin") not in hw.res.owned_by(owner):
            raise ValueError("пин не захвачен проектом")
        if op == "write":
            hw.write(a["pin"], a["value"])
            return None
        if op == "read":
            return hw.value(a["pin"])
        raise ValueError("неизвестная hardware-операция")
