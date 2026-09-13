"use client";

import { useEffect, useRef, useState } from "react";
import { Check } from "lucide-react";
import { Icon } from "@/components/primitives/Icon";
import { cn } from "@/lib/cn";

/**
 * The hero's proof object.
 *
 * DESIGN.md §7.2 wants a Terminal with chrome, step states and copy. The brief
 * warns against a "neon cyan-on-black terminal" (ban list), so this uses the
 * Forge palette: chrome dots in --soot (not the macOS red/amber/green candy),
 * ash text, ox for the active step, and patina ONLY on the verified line -
 * because patina means "genuinely verified" and nothing else (§1.4).
 *
 * The figures shown are jaraco/path's real run: 1 attempt, 6 calls, $0.053.
 * Source: README.md:102-106.
 *
 * Steps animate on enter via IntersectionObserver and fire once. Under
 * prefers-reduced-motion every step renders at its final state immediately.
 */
export type TerminalStep = {
  kind: "cmd" | "info" | "pass" | "verified";
  text: string;
  detail?: string;
};

const STEP_DELAY_MS = 260;

export function Terminal({
  steps,
  title = "vex — fix",
  className,
}: {
  steps: TerminalStep[];
  title?: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [shown, setShown] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(steps.length);
      return;
    }

    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.unobserve(el); // fire once
        let i = 0;
        const tick = () => {
          i += 1;
          setShown(i);
          if (i < steps.length) window.setTimeout(tick, STEP_DELAY_MS);
        };
        window.setTimeout(tick, 220);
      },
      { threshold: 0.25 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [steps.length]);

  return (
    <div
      ref={ref}
      className={cn(
        "overflow-hidden rounded-lg border border-rule bg-char etch",
        className
      )}
    >
      {/* Chrome. Dots are --soot: deliberately NOT the macOS traffic lights. */}
      <div className="flex items-center gap-2 border-b border-rule bg-slab px-4 py-2.5">
        <span className="flex gap-1.5" aria-hidden="true">
          <span className="h-2 w-2 rounded-full bg-soot" />
          <span className="h-2 w-2 rounded-full bg-soot" />
          <span className="h-2 w-2 rounded-full bg-soot" />
        </span>
        <span className="ml-2 font-mono text-mono text-smoke">{title}</span>
      </div>

      <div className="flex flex-col gap-1.5 p-4 font-mono text-mono">
        {steps.slice(0, shown).map((s, i) => (
          <div
            key={i}
            className={cn(
              "flex gap-2",
              s.kind === "verified" && "mt-1 text-verdant-bright",
              s.kind === "pass" && "text-ash",
              s.kind === "info" && "text-smoke",
              s.kind === "cmd" && "text-quench"
            )}
          >
            {s.kind === "verified" ? (
              <Icon
                as={Check}
                size={20}
                className="mt-0.5 text-verdant-bright"
              />
            ) : (
              <span className="select-none text-smoke">
                {s.kind === "cmd" ? "$" : " "}
              </span>
            )}
            <span className="flex-1">{s.text}</span>
            {s.detail ? (
              <span className="shrink-0 text-smoke">{s.detail}</span>
            ) : null}
          </div>
        ))}

        {/* Caret only while steps are still arriving. */}
        {shown < steps.length ? (
          <span
            className="ml-4 inline-block h-4 w-1.5 bg-ox"
            style={{ animation: "vexCaret 1s steps(2) infinite" }}
            aria-hidden="true"
          />
        ) : null}
      </div>

      <style>{`
        @keyframes vexCaret { 0%,49% { opacity: 1 } 50%,100% { opacity: 0 } }
        @media (prefers-reduced-motion: reduce) { .vex-caret { animation: none !important } }
      `}</style>
    </div>
  );
}
