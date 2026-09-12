"use client";

import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * A panel that tilts a few degrees toward the cursor, with a gold sheen
 * tracking the same point — so it reads as a milled plate catching light
 * rather than a card performing a 3D trick.
 *
 * Deliberately restrained at 5 degrees. The ban list forbids hover transforms
 * that shift layout; rotation about the element's own centre does not, and the
 * sheen is a background on an overlay so it never touches layout either.
 */
export function TiltCard({
  children,
  className,
  maxDeg = 5,
}: {
  children: ReactNode;
  className?: string;
  maxDeg?: number;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const sheen = useRef<HTMLDivElement | null>(null);

  const onMove = (e: React.PointerEvent) => {
    const el = ref.current;
    if (!el) return;
    if (!window.matchMedia("(hover: hover)").matches) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width;
    const py = (e.clientY - r.top) / r.height;

    const rx = ((0.5 - py) * maxDeg).toFixed(2);
    const ry = ((px - 0.5) * maxDeg).toFixed(2);
    el.style.transform =
      "perspective(900px) rotateX(" + rx + "deg) rotateY(" + ry + "deg)";

    if (sheen.current) {
      sheen.current.style.opacity = "1";
      sheen.current.style.background =
        "radial-gradient(420px circle at " +
        px * 100 +
        "% " +
        py * 100 +
        "%, rgba(201,169,97,0.10), transparent 62%)";
    }
  };

  const reset = () => {
    if (ref.current) ref.current.style.transform = "";
    if (sheen.current) sheen.current.style.opacity = "0";
  };

  return (
    <div
      ref={ref}
      onPointerMove={onMove}
      onPointerLeave={reset}
      className={cn("relative will-change-transform", className)}
      style={{ transition: "transform 520ms var(--ease-forge)" }}
    >
      {children}
      <div
        ref={sheen}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 rounded-[inherit] opacity-0"
        style={{ transition: "opacity 380ms var(--ease-forge)" }}
      />
    </div>
  );
}
