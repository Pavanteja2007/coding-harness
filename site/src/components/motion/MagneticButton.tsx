"use client";

import { useRef, type ReactNode } from "react";

/**
 * A control that leans toward the cursor as it approaches.
 *
 * Displacement is capped at 6px. The point is that the control feels weighted
 * and responsive — not that it moves. Larger offsets read as a toy, which is
 * why most "magnetic button" implementations look cheap.
 *
 * Only active where a real pointer exists, disabled under reduced motion, and
 * it animates transform only so it can never shift layout.
 */
export function MagneticButton({
  children,
  strength = 0.28,
  max = 6,
}: {
  children: ReactNode;
  strength?: number;
  max?: number;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);

  const onMove = (e: React.PointerEvent) => {
    const el = ref.current;
    if (!el) return;
    if (!window.matchMedia("(hover: hover)").matches) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const r = el.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2);
    const dy = e.clientY - (r.top + r.height / 2);
    const x = Math.max(-max, Math.min(max, dx * strength));
    const y = Math.max(-max, Math.min(max, dy * strength));
    el.style.transform = "translate(" + x + "px, " + y + "px)";
  };

  const reset = () => {
    const el = ref.current;
    if (el) el.style.transform = "translate(0px, 0px)";
  };

  return (
    <span
      ref={ref}
      onPointerMove={onMove}
      onPointerLeave={reset}
      className="inline-block will-change-transform"
      style={{ transition: "transform 380ms var(--ease-forge)" }}
    >
      {children}
    </span>
  );
}
