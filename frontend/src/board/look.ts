import type { Board, Iface, Pin, PinState } from "../api";

export function ifaceShort(iface: Iface): string {
  // uart2_m0 -> UART2 m0, i2c3_m0 -> I2C3 m0
  return iface.id.toUpperCase().replace(/_M(\d)$/, " m$1");
}

export function formatValue(st: PinState | undefined): string | null {
  const v = st?.value;
  if (v === null || v === undefined || !st?.claim) return null;
  if (st.claim.mode === "adc") return `${(v as number).toFixed(3)} В`;
  if (st.claim.mode === "soft_pwm") return `${Math.round((v as number) * 100)}%`;
  return v ? "HIGH" : "LOW";
}

export interface PinLook {
  fill: string;
  stroke: string;
  text: string; // цвет номера внутри
  label: string;
  dim: boolean; // подпись приглушена (питание/GND/служебные)
}

const C = (name: string) => `var(--color-${name})`;

export function pinLook(board: Board, pin: Pin, st: PinState | undefined): PinLook {
  if (pin.power === "5v") return { fill: C("pin-5v"), stroke: C("pin-5v"), text: "#fff", label: "+5V", dim: true };
  if (pin.power === "3v3") return { fill: C("pin-3v3"), stroke: C("pin-3v3"), text: "#1d1d1d", label: "+3V3", dim: true };
  if (pin.power === "gnd") return { fill: C("pin-gnd"), stroke: "#555", text: "#888", label: "GND", dim: true };
  if (pin.special) return { fill: "#3a3a3a", stroke: "#555", text: "#999", label: pin.special, dim: true };

  const iface = board.interfaces.find((i) => i.id === pin.function);
  if (iface) {
    return {
      fill: "#2c2440", stroke: C("pin-iface"), text: "#cbb8f0",
      label: `${ifaceShort(iface)} · ${iface.pins[pin.id]}`, dim: false,
    };
  }

  const label = pin.name ?? pin.id;
  const claim = st?.claim;
  if (pin.kind === "adc") {
    return claim
      ? { fill: C("pin-adc"), stroke: C("pin-adc"), text: "#0d2b27", label, dim: false }
      : { fill: "#203633", stroke: C("pin-adc"), text: "#8fd9cf", label, dim: false };
  }
  if (!claim) return { fill: "#2a2a2a", stroke: C("pin-free"), text: "#aaa", label, dim: false };
  const high = st?.value === true;
  if (claim.mode === "gpio_out")
    return { fill: high ? C("pin-high") : "#1e2c42", stroke: C("pin-out"), text: high ? "#10301a" : "#9db8e0", label, dim: false };
  if (claim.mode === "gpio_in")
    return { fill: high ? C("pin-high") : "#1b302d", stroke: C("pin-in"), text: high ? "#10301a" : "#8fd9cf", label, dim: false };
  return { fill: "#1e2c42", stroke: C("pin-out"), text: "#9db8e0", label, dim: false }; // soft_pwm
}
