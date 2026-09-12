"use client";

import { useEffect, useRef, useState } from "react";
import { VisuallyHidden } from "@/components/primitives/VisuallyHidden";

/**
 * Staggered text reveal.
 *
 * MASTER_BUILD_PROMPT §4 §2 asks for a ~40ms character stagger on the hero h1.
 * DESIGN.md §4.2 caps sibling stagger at 40-70ms and §4.3 sets reveal distance
 * at 12-20px - "Not 60px. Small movement reads expensive."
 *
 * `highlight` renders exactly one word in --color-ox. The spec allows one
 * ox word in a heading, flat, and DESIGN.md §1.3 makes ox the "work is
 * happening" colour. On this site that word is "real", NOT "Verified": §1.4
 * reserves patina for anything verified, so colouring "Verified" ox would
 * contradict the palette's central idea (decision D4).
 *
 * Accessibility: animated spans are aria-hidden and the full string is exposed
 * once via VisuallyHidden, so a screen reader gets one clean heading instead of
 * a letter-by-letter stutter.
 *
 * Reduced motion renders the final state with no timers started at all.
 */
export function SplitText({
  text,
  highlight,
  mode = "chars",
  delayMs = 40,
  durationMs = 700,
  className,
}: {
  text: string;
  /** A word inside `text` to render in ox. Must appear verbatim in text. */
  highlight?: string;
  mode?: "chars" | "words";
  delayMs?: number;
  durationMs?: number;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [active, setActive] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setActive(true);
      setReady(true);
      return;
    }

    const io = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        io.unobserve(el); // fire once only
        setActive(true);
      },
      { threshold: 0.2 }
    );
    io.observe(el);
    setReady(true);
    return () => io.disconnect();
  }, []);

  // Build the unit list, tagging the highlighted word's characters so they can
  // take the ox token while still participating in the same stagger.
  type Unit = { ch: string; hot: boolean };
  const units: Unit[] = [];
  if (mode === "chars") {
    const isHot = (idx: number) => {
      if (!highlight) return false;
      const from = text.indexOf(highlight);
      if (from < 0) return false;
      return idx >= from && idx < from + highlight.length;
    };
    [...text].forEach((ch, i) => units.push({ ch, hot: isHot(i) }));
  } else {
    // Word mode: split on whitespace runs, keep them, mark none as hot per
    // character (a whole highlighted word is safe here).
    text.split(/(\s+)/).forEach((w) => {
      const hot = !!highlight && w === highlight;
      units.push({ ch: w, hot });
    });
  }

  const finalState = !ready || active;

  return (
    <span ref={ref} className={className}>
      <VisuallyHidden>{text}</VisuallyHidden>
      <span aria-hidden="true">
        {units.map((u, i) => {
          if (/^\s+$/.test(u.ch)) {
            return <span key={i}>{u.ch === " " ? " " : u.ch}</span>;
          }
          return (
            <span
              key={i}
              className={u.hot ? "inline-block text-ox-bright" : "inline-block"}
              style={{
                opacity: finalState ? 1 : 0,
                transform: finalState ? "none" : "translateY(14px)",
                transition: `opacity ${durationMs}ms var(--ease-forge) ${
                  i * delayMs
                }ms, transform ${durationMs}ms var(--ease-forge) ${i * delayMs}ms`,
                willChange: active ? "auto" : "opacity, transform",
              }}
            >
              {u.ch}
            </span>
          );
        })}
      </span>
    </span>
  );
}
