"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * Plots any <path data-draw> or <line data-draw> inside it by animating
 * stroke-dashoffset once the group enters view, so a diagram appears to be
 * DRAWN rather than to fade in. Fading is what every template does.
 *
 * Each stroke's real length is measured with getTotalLength(), so this works
 * on arbitrary geometry with no hand-tuned dash values.
 *
 * Under reduced motion nothing is touched, so the diagram is simply already
 * complete — which is the correct final state.
 */
export function DrawSVG({
  children,
  durationMs = 1200,
  stagger = 90,
  className,
}: {
  children: ReactNode;
  durationMs?: number;
  stagger?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const strokes = Array.from(
      host.querySelectorAll<SVGGeometryElement>(
        "path[data-draw], line[data-draw]"
      )
    );
    if (!strokes.length) return;

    strokes.forEach((s) => {
      const len = s.getTotalLength ? s.getTotalLength() : 0;
      if (!len) return;
      s.style.strokeDasharray = String(len);
      s.style.strokeDashoffset = String(len);
    });

    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.unobserve(host);
        strokes.forEach((s, i) => {
          s.style.transition =
            "stroke-dashoffset " +
            durationMs +
            "ms var(--ease-draw) " +
            i * stagger +
            "ms";
          s.style.strokeDashoffset = "0";
        });
      },
      { threshold: 0.25 }
    );

    io.observe(host);
    return () => io.disconnect();
  }, [durationMs, stagger]);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}
