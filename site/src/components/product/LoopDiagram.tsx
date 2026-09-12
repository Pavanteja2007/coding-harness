"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

/**
 * The agent loop, with the verifier gate as the one scroll-linked moment on the
 * page (MASTER_BUILD_PROMPT §4 §5; DESIGN.md §4.3 - "One scroll-linked hero
 * moment per page. Two is a carnival").
 *
 * Hand-authored SVG, not an icon composite (§8: "Product diagrams are
 * hand-authored SVG").
 *
 * The gate is a shutter. As the section scrolls through, the two leaves draw
 * apart and the frame turns from --soot to --patina: the suite passed, so the
 * gate opens. Patina is used here because this is a GENUINELY verified state,
 * which is the only thing §1.4 permits it on.
 *
 * GSAP + ScrollTrigger are imported dynamically so they never enter the initial
 * chunk. Under prefers-reduced-motion the gate renders fully open and neither
 * library is fetched at all.
 */
export function LoopDiagram({ className }: { className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setProgress(1); // resolve to the final state; fetch nothing
      return;
    }

    let ctx: { revert: () => void } | undefined;
    let cancelled = false;

    (async () => {
      const [{ gsap }, { ScrollTrigger }] = await Promise.all([
        import("gsap"),
        import("gsap/ScrollTrigger"),
      ]);
      if (cancelled) return;
      gsap.registerPlugin(ScrollTrigger);

      ctx = gsap.context(() => {
        ScrollTrigger.create({
          trigger: el,
          start: "top 78%",
          end: "bottom 62%",
          scrub: 0.6,
          onUpdate: (self) => setProgress(self.progress),
        });
      }, el);
    })();

    return () => {
      cancelled = true;
      ctx?.revert();
    };
  }, []);

  // Leaves part by up to 34px each; the frame crosses to patina near the end.
  const open = Math.min(1, progress * 1.15);
  const shift = open * 34;
  const passed = progress > 0.72;

  return (
    <div ref={ref} className={cn("w-full", className)}>
      <svg
        viewBox="0 0 720 200"
        className="h-auto w-full"
        role="img"
        aria-label="The agent loop: plan, then step inside a sandbox, then the verifier gate, then git-native output. The gate opens only when the suite passes."
      >
        <defs>
          <marker
            id="vex-arrow"
            viewBox="0 0 10 10"
            refX="8"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
          </marker>
        </defs>

        {/* rail */}
        <line
          x1="24" y1="100" x2="696" y2="100"
          stroke="var(--color-rule)" strokeWidth="1"
        />

        {/* --- 1. Plan --- */}
        <g className="text-soot" color="var(--color-soot)">
          <rect x="24" y="76" width="116" height="48" rx="6"
                fill="var(--color-slab)" stroke="var(--color-rule)" />
          <text x="82" y="105" textAnchor="middle"
                className="font-mono" fontSize="13"
                fill="var(--color-ash)">plan</text>
          <line x1="140" y1="100" x2="196" y2="100"
                stroke="var(--color-rule)" strokeWidth="1"
                markerEnd="url(#vex-arrow)" />
        </g>

        {/* --- 2. Step (sandboxed) --- */}
        <g>
          <rect x="204" y="68" width="150" height="64" rx="6"
                fill="var(--color-slab)" stroke="var(--color-rule)" />
          <text x="279" y="95" textAnchor="middle"
                className="font-mono" fontSize="13"
                fill="var(--color-ash)">step</text>
          <text x="279" y="115" textAnchor="middle"
                className="font-mono" fontSize="11"
                fill="var(--color-soot)">sandboxed</text>
          <line x1="354" y1="100" x2="410" y2="100"
                stroke="var(--color-rule)" strokeWidth="1"
                markerEnd="url(#vex-arrow)" color="var(--color-soot)" />
        </g>

        {/* --- 3. THE GATE (the scroll-linked moment) --- */}
        <g>
          {/* frame */}
          <rect
            x="418" y="52" width="124" height="96" rx="8"
            fill="none"
            stroke={passed ? "var(--color-verdant)" : "var(--color-soot)"}
            strokeWidth="1.5"
            style={{ transition: "stroke 420ms var(--ease-forge)" }}
          />
          {/* leaves: they part as progress advances */}
          <g>
            <rect
              x="426" y="60" width="50" height="80" rx="4"
              fill="var(--color-char)"
              stroke="var(--color-rule)"
              style={{ transform: `translateX(${-shift}px)` }}
            />
            <rect
              x="484" y="60" width="50" height="80" rx="4"
              fill="var(--color-char)"
              stroke="var(--color-rule)"
              style={{ transform: `translateX(${shift}px)` }}
            />
          </g>
          {/* what the gate reveals: the passing suite */}
          <text
            x="480" y="105" textAnchor="middle"
            className="font-mono" fontSize="12"
            fill="var(--color-verdant-bright)"
            style={{ opacity: open, transition: "opacity 300ms linear" }}
          >
            suite green
          </text>
          <text x="480" y="170" textAnchor="middle"
                className="font-mono" fontSize="12"
                fill={passed ? "var(--color-verdant)" : "var(--color-smoke)"}
                style={{ transition: "fill 420ms var(--ease-forge)" }}>
            verify
          </text>

          <line x1="542" y1="100" x2="598" y2="100"
                stroke="var(--color-rule)" strokeWidth="1"
                markerEnd="url(#vex-arrow)" color="var(--color-soot)"
                style={{ opacity: 0.35 + open * 0.65 }} />
        </g>

        {/* --- 4. git-native output --- */}
        <g style={{ opacity: 0.35 + open * 0.65, transition: "opacity 300ms linear" }}>
          <rect x="606" y="76" width="90" height="48" rx="6"
                fill="var(--color-slab)"
                stroke={passed ? "var(--color-verdant)" : "var(--color-rule)"}
                style={{ transition: "stroke 420ms var(--ease-forge)" }} />
          <text x="651" y="105" textAnchor="middle"
                className="font-mono" fontSize="12"
                fill="var(--color-ash)">branch</text>
        </g>

        {/* the loop-back: a failed verify returns to step */}
        <path
          d="M 480 148 L 480 182 L 279 182 L 279 132"
          fill="none"
          stroke="var(--color-rule)"
          strokeWidth="1"
          strokeDasharray="3 4"
          markerEnd="url(#vex-arrow)"
          color="var(--color-soot)"
          style={{ opacity: 1 - open * 0.55 }}
        />
        <text x="380" y="196" textAnchor="middle"
              className="font-mono" fontSize="11"
              fill="var(--color-soot)"
              style={{ opacity: 1 - open * 0.55 }}>
          fails → retry
        </text>
      </svg>
    </div>
  );
}
