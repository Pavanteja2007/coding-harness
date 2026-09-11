"use client";

/**
 * Client boundary for the hero shader.
 *
 * next/dynamic with `ssr: false` is NOT allowed inside a Server Component, so
 * this thin client component exists purely to host that dynamic import. Keeping
 * the boundary this narrow means the hero's copy, headings, and CTAs stay
 * server-rendered - only the canvas costs client JS.
 */
import dynamic from "next/dynamic";

const ShaderHero = dynamic(() => import("./ShaderHero"), { ssr: false });

export function ShaderHeroClient({ className }: { className?: string }) {
  return <ShaderHero className={className} />;
}
