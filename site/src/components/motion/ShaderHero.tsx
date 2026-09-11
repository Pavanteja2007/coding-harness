"use client";

/**
 * Host for the Forge hero shader.
 *
 * Loaded via next/dynamic({ ssr: false }) so ogl never enters the server
 * bundle or the initial client chunk. Renders the static WebP fallback as the
 * base layer and mounts the canvas above it, so the "no WebGL" rung needs no
 * branch: if mountForge() returns null, the image simply stays visible.
 *
 * The fallback is a real server-rendered <img>, not a gradient, so it costs
 * no JS and is present in the very first paint.
 */
import { useEffect, useRef, useState } from "react";
import { mountForge, type ForgeHandle } from "./shader/mountForge";
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
      handle = mountForge(host, { reducedMotion, mobile });
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
      {/* Rung: no WebGL / context lost. Always painted; the canvas covers it. */}
      <img
        src="/hero-fallback.webp"
        alt=""
        width={1600}
        height={1000}
        className="absolute inset-0 h-full w-full object-cover"
        style={{ opacity: failed ? 1 : 0, transition: "opacity 600ms" }}
      />

      {/* Rung: WebGL. */}
      <div ref={hostRef} className="absolute inset-0 h-full w-full" />

      {/* Scrim so the headline clears 4.5:1 against the BRIGHTEST frame,
          not the average - DESIGN.md §3.4.5. */}
      <div
        className="absolute inset-0 z-[1]"
        style={{
          background:
            "linear-gradient(180deg, rgba(8,7,6,0.32) 0%, rgba(8,7,6,0.55) 42%, rgba(8,7,6,0.92) 78%, #080706 100%)",
        }}
      />

      <GrainOverlay className={failed ? "grain-hold" : ""} />
    </div>
  );
}
