"use client";

import { cn } from "@/lib/cn";

/**
 * A slow continuous strip, used for the built-with stack.
 *
 * Duplicating the row and translating 50% gives a seamless loop with zero JS.
 * Paused on hover so anything in it stays readable, held still under reduced
 * motion, and masked at both ends so it dissolves rather than being chopped.
 */
export function Marquee({
  items,
  durationS = 46,
  className,
}: {
  items: string[];
  durationS?: number;
  className?: string;
}) {
  const row = (
    <div className="flex shrink-0 items-center gap-3 pr-3" aria-hidden="true">
      {items.map((s) => (
        <span
          key={s}
          className="whitespace-nowrap rounded-sm border border-rule bg-slab px-4 py-2 font-mono text-mono text-ash"
        >
          {s}
        </span>
      ))}
    </div>
  );

  return (
    <div
      className={cn(
        "group relative overflow-hidden",
        "[mask-image:linear-gradient(90deg,transparent,#000_9%,#000_91%,transparent)]",
        className
      )}
    >
      {/* The real list stays in the accessibility tree once, unanimated. */}
      <ul className="sr-only">
        {items.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>

      <div
        className="flex w-max group-hover:[animation-play-state:paused] motion-reduce:animate-none"
        style={{ animation: "vexMarquee " + durationS + "s linear infinite" }}
      >
        {row}
        {row}
      </div>

      <style>{`
        @keyframes vexMarquee {
          from { transform: translateX(0); }
          to   { transform: translateX(-50%); }
        }
      `}</style>
    </div>
  );
}
