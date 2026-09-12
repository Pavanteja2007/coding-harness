"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * Scroll-coupled translate.
 *
 * This is AMBIENT depth, not choreography — it does not consume the single
 * scroll-linked "moment" DESIGN.md §4.3 permits, which belongs to the verifier
 * gate. Kept slow and small so it reads as parallax rather than as movement.
 *
 * transform only, rAF-driven, paused when offscreen, off under reduced motion.
 */
export function Parallax({
  children,
  speed = 0.06,
  className,
}: {
  children: ReactNode;
  speed?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let raf = 0;
    let visible = false;

    const tick = () => {
      raf = 0;
      if (!visible) return;
      const r = el.getBoundingClientRect();
      const centre = r.top + r.height / 2 - window.innerHeight / 2;
      el.style.transform =
        "translate3d(0, " + (-centre * speed).toFixed(2) + "px, 0)";
      raf = requestAnimationFrame(tick);
    };

    const io = new IntersectionObserver(([e]) => {
      visible = e.isIntersecting;
      if (visible && !raf) raf = requestAnimationFrame(tick);
    });
    io.observe(el);

    return () => {
      io.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, [speed]);

  return (
    <div ref={ref} className={className} style={{ willChange: "transform" }}>
      {children}
    </div>
  );
}
