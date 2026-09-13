import { ImageResponse } from "next/og";
import { site } from "@/lib/site";

/**
 * The Open Graph image, generated rather than drawn.
 *
 * Next renders this at build time, so it is produced from the same tokens the
 * site uses. A hand-exported PNG would have to be re-cut by hand every time
 * the palette moves - and this palette has moved four times.
 *
 * Deliberately restrained: the wordmark, the headline, and the install
 * command. No fabricated metrics, no logos, nothing that is not on the page.
 */
export const runtime = "edge";
export const alt = site.tagline;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Tokens, inline: ImageResponse has no access to the stylesheet.
const INK = "#0A0708";
const OX = "#8C1B33";
const OX_BRIGHT = "#D4607A";
const QUENCH = "#F2EFEA";
const ASH = "#A39B9C";
const SOOT = "#453D40";
const RULE = "#2E2428";

export default async function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: INK,
          padding: "72px 80px",
          // A faint oxblood wash from the upper right, echoing the hero field.
          backgroundImage: `radial-gradient(120% 100% at 88% 8%, rgba(140,27,51,0.30) 0%, rgba(10,7,8,0) 62%)`,
        }}
      >
        {/* wordmark */}
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <div
            style={{
              fontSize: 40,
              color: QUENCH,
              letterSpacing: "-0.02em",
            }}
          >
            vex
          </div>
          <div style={{ width: 1, height: 28, background: RULE }} />
          <div style={{ fontSize: 20, color: SOOT }}>
            cli-first / verifier-gated / open source
          </div>
        </div>

        {/* headline */}
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              fontSize: 82,
              lineHeight: 1.04,
              letterSpacing: "-0.03em",
              color: QUENCH,
            }}
          >
            <span>Fix&nbsp;</span>
            <span style={{ color: OX_BRIGHT }}>real</span>
            <span>&nbsp;bugs. Verified, not vibed.</span>
          </div>

          <div
            style={{
              fontSize: 26,
              color: ASH,
              maxWidth: 820,
              lineHeight: 1.45,
            }}
          >
            Plans a fix, edits in a Docker sandbox, and runs your real test
            suite. Success only when the suite agrees.
          </div>
        </div>

        {/* install command */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 16,
            padding: "22px 28px",
            border: `1px solid ${OX}`,
            borderRadius: 10,
            background: "rgba(28,22,25,0.85)",
            alignSelf: "flex-start",
          }}
        >
          <span style={{ color: SOOT, fontSize: 26 }}>$</span>
          <span style={{ color: QUENCH, fontSize: 26 }}>
            pip install vex-harness
          </span>
        </div>
      </div>
    ),
    size
  );
}
