"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Count-up for stat tiles.
 *
 * Fires once on enter (DESIGN.md §4.3: "Reveals fire once"). Under
 * prefers-reduced-motion the final value renders immediately with no timer
 * started at all - the number is information, so it must never be withheld.
 *
 * Uses tabular figures via the .tnum class so digits do not jitter as they
 * change width mid-count.
 */
export function CountUp({
  value,
  decimals = 0,
  prefix = "",
  suffix = "",
  durationMs = 900,
  className,
}: {
  value: number;
  decimals?: number;
  prefix?: string;
  suffix?: string;
  durationMs?: number;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [shown, setShown] = useState<number | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(value);
      return;
    }

    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.unobserve(el); // once only

        const start = performance.now();
        const tick = (now: number) => {
          const t = Math.min(1, (now - start) / durationMs);
          // Matches --ease-forge: fast in, settles slow.
          const eased = 1 - Math.pow(1 - t, 3);
          setShown(value * eased);
          if (t < 1) requestAnimationFrame(tick);
          else setShown(value);
        };
        requestAnimationFrame(tick);
      },
      { threshold: 0.4 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [value, durationMs]);

  const display =
    shown === null
      ? (0).toFixed(decimals)
      : shown.toFixed(decimals);

  return (
    <span ref={ref} className={className}>
      {prefix}
      {Number(display).toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })}
      {suffix}
    </span>
  );
}
