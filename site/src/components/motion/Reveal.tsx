"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

/**
 * Intersection-Observer reveal wrapper. The workhorse entrance for every
 * section.
 *
 * DESIGN.md §4: transform + opacity only, reveal distance 12-20px ("Not 60px.
 * Small movement reads expensive"), fires ONCE (unobserve after), enters on
 * --ease-forge (never ease-in-out). Under prefers-reduced-motion it renders at
 * the final state with no transition.
 *
 * `delay` staggers siblings; keep it in the 40-70ms range per §4.2.
 */
export function Reveal({
  children,
  delay = 0,
  y = 16,
  className,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  delay?: number;
  y?: number;
  className?: string;
  as?: React.ElementType;
}) {
  const ref = useRef<HTMLElement | null>(null);
  const [shown, setShown] = useState(false);
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setReduced(true);
      setShown(true);
      return;
    }

    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.unobserve(el);
        setShown(true);
      },
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <Tag
      ref={ref}
      className={className}
      style={{
        opacity: shown ? 1 : 0,
        transform: shown ? "none" : `translateY(${y}px)`,
        transition: reduced
          ? "none"
          : `opacity 620ms var(--ease-forge) ${delay}ms, transform 620ms var(--ease-forge) ${delay}ms`,
        willChange: shown ? "auto" : "opacity, transform",
      }}
    >
      {children}
    </Tag>
  );
}
