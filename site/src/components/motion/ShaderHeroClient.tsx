"use client";

/**
 * Client boundary for the hero's verification field.
 *
 * Canvas 2D, so there is no shader compile step, no WebGL context to lose, and
 * no fallback ladder to maintain - the previous ogl heroes needed six rungs;
 * this needs none. Still dynamically imported so the animation code stays out
 * of the initial chunk.
 */
import dynamic from "next/dynamic";

const VerificationField = dynamic(() => import("./VerificationField"), {
  ssr: false,
});

export function ShaderHeroClient({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 overflow-hidden"
    >
      <VerificationField className={className} />

      {/* Scrim: heavy behind the copy on the left, open on the right so the
          graph stays legible. Measured, not guessed - see the contrast check. */}
      <div
        className="absolute inset-0 z-[1]"
        style={{
          background:
            "radial-gradient(112% 92% at 20% 46%, rgba(10,7,8,0.95) 0%, rgba(10,7,8,0.86) 26%, rgba(10,7,8,0.40) 58%, rgba(10,7,8,0.06) 100%)",
        }}
      />
      <div
        className="absolute inset-x-0 bottom-0 z-[2] h-56"
        style={{ background: "linear-gradient(180deg, transparent, #0A0708 92%)" }}
      />
    </div>
  );
}
