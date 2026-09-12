"use client";

import { useEffect, useState } from "react";

/**
 * Scroll progress rail, and Lenis smooth scroll.
 *
 * The rail is a 2px ox line across the top of the viewport. It is the only
 * always-on motion outside the shader, and it is information rather than
 * decoration: it says how far through the argument you are.
 *
 * Lenis is initialised here too, at a subtle lerp (~0.08 per the stack note).
 * Both are disabled entirely under prefers-reduced-motion - smooth scroll
 * hijacks the scroll a person asked their OS to keep simple.
 */
export function ScrollProgress() {
  const [pct, setPct] = useState(0);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const onScroll = () => {
      const doc = document.documentElement;
      const max = doc.scrollHeight - doc.clientHeight;
      setPct(max > 0 ? (doc.scrollTop / max) * 100 : 0);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });

    let lenis: { raf: (t: number) => void; destroy: () => void } | undefined;
    let raf = 0;
    let cancelled = false;

    if (!reduced) {
      (async () => {
        const { default: Lenis } = await import("lenis");
        if (cancelled) return;
        lenis = new Lenis({ lerp: 0.08, wheelMultiplier: 1 });
        const loop = (t: number) => {
          lenis?.raf(t);
          raf = requestAnimationFrame(loop);
        };
        raf = requestAnimationFrame(loop);
      })();
    }

    return () => {
      cancelled = true;
      window.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
      lenis?.destroy();
    };
  }, []);

  return (
    <div
      aria-hidden="true"
      className="fixed inset-x-0 top-0 z-[70] h-0.5 bg-transparent"
    >
      <div
        className="h-full bg-ox"
        style={{ width: `${pct}%`, transition: "width 90ms linear" }}
      />
    </div>
  );
}
