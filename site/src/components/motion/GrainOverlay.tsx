/**
 * Film grain over the shader - DESIGN.md §3.4.3 calls this "the single
 * highest-leverage expensive signal", and it is what kills the plastic CGI
 * look. Mandatory at 3-5% opacity.
 *
 * Pure CSS: an inline SVG feTurbulence filter tiled as a data-URI background
 * and jittered by animating background-position. Deliberately NOT a per-frame
 * canvas regeneration, which would cost CPU every frame for an effect that is
 * indistinguishable at 4% opacity. Costs zero JS, which matters against a
 * 180 kB budget.
 *
 * The data-URI should be generated once and inlined as a constant rather than
 * built at render time.
 */
const GRAIN_SVG =
  "<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'>" +
  "<filter id='n'>" +
  "<feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/>" +
  "</filter>" +
  "<rect width='100%' height='100%' filter='url(%23n)'/>" +
  "</svg>";

const GRAIN_URL = `url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(
  GRAIN_SVG
).replace(/%2523/g, "%23")}")`;

export function GrainOverlay({ className = "" }: { className?: string }) {
  return (
    <>
      <div
        aria-hidden="true"
        className={`pointer-events-none absolute inset-0 z-[2] mix-blend-overlay ${className}`}
        style={{
          backgroundImage: GRAIN_URL,
          opacity: 0.042,
          animation: "grainShift 1.1s steps(4) infinite",
        }}
      />
      <style>{`
        @keyframes grainShift {
          0%   { background-position: 0 0; }
          25%  { background-position: -14px 9px; }
          50%  { background-position: 11px -13px; }
          75%  { background-position: -9px -11px; }
          100% { background-position: 7px 12px; }
        }
        @media (prefers-reduced-motion: reduce) {
          /* The grain stays - it is texture, not motion - it simply holds. */
          .grain-hold { animation: none !important; }
        }
      `}</style>
    </>
  );
}
