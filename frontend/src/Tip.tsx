/** Подсказка при наведении/фокусе: сразу, без задержки системного title. */
export function Tip({ text, children, className = "" }: {
  text: string; children: React.ReactNode; className?: string;
}) {
  return (
    <span className={`group/tip relative ${className}`}>
      {children}
      <span role="tooltip"
            className="pointer-events-none absolute top-full right-0 z-20 mt-1 hidden w-max max-w-64 rounded border border-line
                       bg-bg px-2 py-1 text-xs leading-snug whitespace-normal text-fg shadow-lg
                       group-hover/tip:block group-focus-within/tip:block">
        {text}
      </span>
    </span>
  );
}
