/**
 * The Forge hero shader - domain-warped fractional Brownian motion.
 *
 * DESIGN.md §3.1: the noise field is sampled at coordinates that are
 * themselves offset by another noise field, twice (Inigo Quilez's warping
 * technique). That produces slow folding liquid-metal structure no CSS
 * gradient can imitate.
 *
 * Craft rules enforced here (DESIGN.md §3.4):
 *  1. Slow       - full cycle 40-60s (uTime is scaled right down)
 *  2. Desaturated- output is mostly --ink -> --scorch; --copper appears only
 *                  at the crests, and never --flare across a field
 *  3. Grain      - NOT here; it is a cheap CSS overlay in GrainOverlay.tsx
 *  4. Vignette   - radial darkening to --ink at the edges
 *  5. Contrast   - a scrim sits behind the text (Hero section), so the
 *                  headline clears 4.5:1 against the BRIGHTEST frame
 *  6. Never full-viewport-bright - fades out by ~70vh
 *
 * Colours are hardcoded as GLSL vec3s in linear-ish sRGB space rather than
 * passed as uniforms, because one draw call with no per-frame upload is
 * cheaper and these never change. They are the DESIGN.md §1 tokens:
 *   ink #080706, basalt #0F0D0B, scorch #7A3F24, copper #C56A3E
 */

export const VERT = /* glsl */ `
attribute vec2 uv;
attribute vec2 position;
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position, 0, 1);
}
`;

export const FRAG = /* glsl */ `
precision highp float;

uniform float uTime;
uniform vec2  uResolution;

varying vec2 vUv;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
    mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
    u.y
  );
}

// 5-octave fBm. The rotation decorrelates successive octaves so they do not
// line up into visible grid artefacts.
float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.5;
  mat2 rot = mat2(0.80, 0.60, -0.60, 0.80);
  for (int i = 0; i < 5; i++) {
    v += a * noise(p);
    p = rot * p * 2.02;
    a *= 0.5;
  }
  return v;
}

void main() {
  float aspect = uResolution.x / max(uResolution.y, 1.0);
  vec2 p = vec2(vUv.x * aspect, vUv.y) * 2.6;
  float t = uTime;

  // Domain warp, applied twice - DESIGN.md §3.1.
  vec2 q = vec2(
    fbm(p + t * 0.06),
    fbm(p + vec2(5.2, 1.3) + t * 0.06)
  );
  vec2 r = vec2(
    fbm(p + 1.7 * q + vec2(1.7, 9.2) + t * 0.04),
    fbm(p + 1.7 * q + vec2(8.3, 2.8) + t * 0.04)
  );
  float f = fbm(p + 1.7 * r);

  vec3 ink    = vec3(0.031, 0.027, 0.024);
  vec3 basalt = vec3(0.059, 0.051, 0.043);
  vec3 scorch = vec3(0.478, 0.247, 0.141);
  vec3 copper = vec3(0.773, 0.416, 0.243);

  // Band the field. fBm here occupies roughly [0.25, 0.75] and its mean sits
  // near 0.5, so the thresholds are calibrated onto that range rather than
  // onto an assumed [0,1]. Getting this wrong is what makes a shader like this
  // read as a copper FIELD instead of dark metal with heat at the crests.
  float band = clamp((f - 0.30) * 1.9 + length(r) * 0.16, 0.0, 1.0);

  // Mostly ink -> basalt. DESIGN.md §3.4.2.
  vec3 col = mix(ink, basalt, smoothstep(0.00, 0.55, band));
  // Scorch is the body of the heat; it must not dominate. Capped at 0.68.
  col = mix(col, scorch, smoothstep(0.58, 0.90, band) * 0.68);
  // Copper ONLY at the crests, and only 30% of the way there.
  col = mix(col, copper, smoothstep(0.86, 1.00, band) * 0.30);

  // Vignette to ink at the edges so the shader never fights the nav or the
  // headline (DESIGN.md §3.4.4). Strong: text must clear 4.5:1 against the
  // BRIGHTEST possible frame, so the corners have to fall away hard.
  vec2 c = vUv - 0.5;
  c.x *= aspect;
  col = mix(ink, col, smoothstep(1.05, 0.15, length(c)) * 0.92);

  // Fade the lower edge into the page ground - §3.4.6, upper ~70vh only.
  col = mix(ink, col, smoothstep(0.0, 0.42, vUv.y));

  gl_FragColor = vec4(col, 1.0);
}
`;
