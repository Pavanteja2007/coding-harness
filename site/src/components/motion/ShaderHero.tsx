"use client";

/**
 * Host for the infinite-descent fractal.
 *
 * The shader owns the FULL viewport here, not a band — the descent only reads
 * as endless if it fills the frame. Copy sits over it inside a scrim that is
 * heavy enough to guarantee contrast against the brightest possible frame.
 *
 * Loaded via next/dynamic({ ssr: false }) so ogl never enters the server
 * bundle. If mountFractal returns null the static image simply remains.
 */
import { useEffect, useRef, useState } from "react";
import { mountFractal, type ForgeHandle } from "./shader/mountFractal";
import { GrainOverlay } from "./GrainOverlay";

export default function ShaderHero({ className = "" }: { className?: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    const mobile = window.matchMedia("(max-width: 768px)").matches;

    let handle: ForgeHandle | null = null;
    try {
      handle = mountFractal(host, { reducedMotion, mobile });
    } catch {
      handle = null;
    }
    if (!handle) {
      setFailed(true);
      return;
    }
    return () => handle?.destroy();
  }, []);

  return (
    <div
      aria-hidden="true"
      className={`pointer-events-none absolute inset-0 overflow-hidden ${className}`}
    >
      <img
        src="/hero-fallback.webp"
        alt=""
        width={1600}
        height={1000}
        className="absolute inset-0 h-full w-full object-cover"
        style={{ opacity: failed ? 1 : 0, transition: "opacity 600ms" }}
      />

      <div ref={hostRef} className="absolute inset-0 h-full w-full" />

      {/* Scrim. Radial rather than linear: it darkens behind the copy on the
          left while leaving the descent bright and open on the right. */}
      <div
        className="absolute inset-0 z-[1]"
        style={{
          background:
            "radial-gradient(115% 95% at 20% 48%, rgba(9,9,11,0.96) 0%, rgba(9,9,11,0.88) 28%, rgba(9,9,11,0.42) 58%, rgba(9,9,11,0.08) 100%)",
        }}
      />
      {/* Bottom fade into the page ground so the section ends cleanly. */}
      <div
        className="absolute inset-x-0 bottom-0 z-[2] h-56"
        style={{
          background: "linear-gradient(180deg, transparent, #09090B 92%)",
        }}
      />

      <GrainOverlay className={failed ? "grain-hold" : ""} />
    </div>
  );
}
