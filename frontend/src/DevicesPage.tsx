import { ModulesPage } from "./ModulesPage";
import { KernelDriversPage } from "./KernelDriversPage";
import { AdafruitPage } from "./AdafruitPage";
import type { Board } from "./api";

export function DevicesPage({
  section = "adafruit",
  board,
  onError,
}: {
  section?: string;
  board: Board | null;
  onError: (message: string) => void;
}) {
  return (
    <div className="min-h-0 w-full overflow-auto p-6 text-sm">
      <nav
        aria-label="Устройства"
        className="mb-5 flex gap-5 border-b border-line pb-3"
      >
        {[
          ["modules", "Модули"],
          ["adafruit", "Adafruit (CircuitPython)"],
          ["drivers", "Ядерные драйверы"],
        ].map(([key, title]) => (
          <a
            key={key}
            href={`#/devices/${key}`}
            aria-current={section === key ? "page" : undefined}
            className={section === key ? "text-accent" : "text-muted"}
          >
            {title}
          </a>
        ))}
      </nav>
      {section === "modules" ? (
        <ModulesPage />
      ) : section === "drivers" ? (
        <KernelDriversPage onError={onError} />
      ) : (
        <AdafruitPage board={board} onError={onError} />
      )}
    </div>
  );
}
