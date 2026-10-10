import type { Board, PinState } from "../api";
import { formatValue, pinLook } from "./look";

const P = 26; // шаг пинов
const R = 9;
const LABEL_W = 132;
const TITLE_H = 34;
const HEADER_W = LABEL_W * 2 + P + 2 * 16;
const GAP = 48;

interface Props {
  board: Board;
  live: Record<string, PinState>;
  selected: string | null;
  highlight: Set<string>;
  onSelect: (pinId: string) => void;
}

export function BoardSvg({ board, live, selected, highlight, onSelect }: Props) {
  const rows = Math.max(...board.headers.map((h) => h.rows));
  const width = board.headers.length * HEADER_W + (board.headers.length - 1) * GAP;
  const height = TITLE_H + rows * P + 12;
  const byId = new Map(board.pins.map((p) => [p.id, p]));

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full max-w-[860px] select-none"
         role="img" aria-label={`Гребёнки ${board.headers.map((h) => h.id).join(", ")}`}>
      {board.headers.map((h, hi) => {
        const x0 = hi * (HEADER_W + GAP);
        const cxL = x0 + LABEL_W + 16;
        const cxR = cxL + P;
        return (
          <g key={h.id}>
            <text x={cxL + P / 2} y={16} textAnchor="middle" fill="var(--color-fg)"
                  fontSize={14} fontWeight={600}>{h.id}</text>
            {/* корпус разъёма */}
            <rect x={cxL - R - 5} y={TITLE_H - R - 5} width={P + 2 * R + 10} height={(h.rows - 1) * P + 2 * R + 10}
                  rx={4} fill="#202020" stroke="var(--color-line)" />
            {Array.from({ length: h.rows * h.cols }, (_, i) => {
              const n = i + 1;
              const id = `${h.id}-${n}`;
              const pin = byId.get(id);
              if (!pin) return null;
              const st = { ...pin, ...live[id] };
              const look = pinLook(board, pin, st);
              const left = i % 2 === 0;
              const cx = left ? cxL : cxR;
              const cy = TITLE_H + Math.floor(i / 2) * P;
              const value = formatValue(st);
              const isSel = selected === id;
              const isHl = highlight.has(id);
              const lx = left ? cxL - R - 10 : cxR + R + 10;
              return (
                <g key={id} className="cursor-pointer outline-none [&:focus-visible>circle]:stroke-white"
                   role="button" tabIndex={0} aria-pressed={isSel}
                   aria-label={`${id} ${look.label}${value ? ` ${value}` : ""}`}
                   onClick={() => onSelect(id)}
                   onKeyDown={(e) => {
                     if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(id); }
                   }}>
                  <title>{`${id} · ${look.label}`}</title>
                  {/* хит-зона: пин + подпись */}
                  <rect x={left ? x0 : cxR - P / 2} y={cy - P / 2} width={LABEL_W + 16 + P / 2 + R} height={P}
                        fill="transparent" />
                  {(isSel || isHl) && (
                    <circle cx={cx} cy={cy} r={R + 4} fill="none"
                            stroke={isSel ? "#fff" : "var(--color-pin-iface)"} strokeWidth={isSel ? 2 : 1.5}
                            strokeDasharray={isSel ? undefined : "3 2"} />
                  )}
                  {n === 1 ? (
                    <rect x={cx - R} y={cy - R} width={2 * R} height={2 * R} rx={2}
                          fill={look.fill} stroke={look.stroke} strokeWidth={1.5} />
                  ) : (
                    <circle cx={cx} cy={cy} r={R} fill={look.fill} stroke={look.stroke} strokeWidth={1.5} />
                  )}
                  <text x={cx} y={cy + 3} textAnchor="middle" fontSize={8.5} fill={look.text}
                        fontFamily="var(--font-mono)">{n}</text>
                  <text x={lx} y={cy + 4} textAnchor={left ? "end" : "start"} fontSize={11}
                        fontFamily="var(--font-mono)"
                        fill={look.dim ? "var(--color-muted)" : isSel ? "#fff" : "var(--color-fg)"}>
                    {left && value && <tspan fill="var(--color-pin-high)" fontWeight={600}>{value}  </tspan>}
                    {look.label}
                    {!left && value && <tspan fill="var(--color-pin-high)" fontWeight={600}>  {value}</tspan>}
                  </text>
                </g>
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}
