"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

/**
 * Text that resolves out of noise, character by character.
 *
 * Used on eyebrows and small labels ONLY — never the h1. A headline that
 * scrambles is a gimmick that delays the one thing a reader came for. On a
 * six-character label it reads as an instrument settling on a value.
 *
 * Under prefers-reduced-motion the final text renders immediately and no
 * interval is ever created.
 */
const GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/\\<>[]{}=+*#%$";

export function ScrambleText({
  text,
  className,
  speedMs = 34,
  revealEveryFrames = 2,
}: {
  text: string;
  className?: string;
  speedMs?: number;
  revealEveryFrames?: number;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [out, setOut] = useState(text);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let timer: number | undefined;
    let frame = 0;
    let settled = 0;

    const io = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      io.unobserve(el);

      timer = window.setInterval(() => {
        frame += 1;
        if (frame % revealEveryFrames === 0) settled += 1;

        if (settled >= text.length) {
          setOut(text);
          window.clearInterval(timer);
          return;
        }
        const head = text.slice(0, settled);
        const tail = text
          .slice(settled)
          .split("")
          .map((ch) => (ch === " " ? " " : GLYPHS[(Math.random() * GLYPHS.length) | 0]))
          .join("");
        setOut(head + tail);
      }, speedMs);
    });

    io.observe(el);
    return () => {
      io.disconnect();
      if (timer) window.clearInterval(timer);
    };
  }, [text, speedMs, revealEveryFrames]);

  return (
    <span ref={ref} className={cn("tabular-nums", className)}>
      {/* The real string stays in the accessibility tree; the scramble is
          decorative and must never reach a screen reader mid-flight. */}
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">{out}</span>
    </span>
  );
}
