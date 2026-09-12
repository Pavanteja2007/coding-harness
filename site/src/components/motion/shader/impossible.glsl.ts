/**
 * THE IMPOSSIBLE OBJECT — raymarched Penrose tribar in forged steel.
 *
 * WHY THIS OBJECT
 * An impossible figure is locally consistent at every joint and globally
 * cannot exist. That is exactly what an unverified agent claim is: the plan
 * looks right, the edit looks right, the explanation looks right, and the
 * whole thing still does not hold together. The verifier is what reveals the
 * seam. So the hero is not decoration — it is the thesis, rendered.
 *
 * THE GEOMETRY IS REAL 3D, NOT A 2D CHEAT
 * Three beams run along the axes from the origin:
 *     O(0,0,0) --+X--> A(L,0,0) --+Y--> B(L,L,0) --+Z--> C(L,L,L)
 * C - O = L*(1,1,1), which is parallel to the view axis d = normalize(1,1,1).
 * Under an ORTHOGRAPHIC camera along d, C therefore projects onto exactly the
 * same screen point as O, so the far end of the third beam lands on the near
 * end of the first and occludes it. The triangle appears closed. Nothing is
 * faked: the solid really is three disconnected bars, and the impossibility
 * lives entirely in the projection — which is where it lives in reality too.
 *
 * Rotating about d preserves the illusion (it only spins the projection).
 * Rotating off d destroys it — which is the interaction: the cursor tilts the
 * camera by at most ~2.4 degrees, just enough to open the seam.
 *
 * MATERIAL
 * Damascus: a warped field quantised into strata, triplanar-mapped so the
 * layers flow along the bars, lit with Kajiya-Kay anisotropic specular so the
 * highlight rakes ALONG the grain like real forged metal. Gold appears only as
 * light on steel, never as pigment.
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
uniform vec2  uPointer;   // -1..1, smoothed
uniform float uReveal;    // 0..1 entrance
uniform float uFocus;     // 0..1 how centred the object is (mobile vs desktop)

varying vec2 vUv;

// ------------------------------------------------------------------ noise --
vec2 hash2(vec2 p) {
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}
float gnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
  return mix(
    mix(dot(hash2(i + vec2(0,0)), f - vec2(0,0)),
        dot(hash2(i + vec2(1,0)), f - vec2(1,0)), u.x),
    mix(dot(hash2(i + vec2(0,1)), f - vec2(0,1)),
        dot(hash2(i + vec2(1,1)), f - vec2(1,1)), u.x), u.y);
}
float fbm(vec2 p) {
  float v = 0.0, a = 0.5;
  mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 5; i++) { v += a * gnoise(p); p = rot * p * 2.03; a *= 0.5; }
  return v;
}

// -------------------------------------------------------------------- sdf --
float sdBox(vec3 p, vec3 b) {
  vec3 q = abs(p) - b;
  return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0);
}

vec3 rotAxis(vec3 p, vec3 axis, float a) {
  float c = cos(a), s = sin(a);
  return p * c + cross(axis, p) * s + axis * dot(axis, p) * (1.0 - c);
}

const float L  = 1.0;    // beam length
const float TH = 0.115;  // thin: the interior void must read as clearly as the bars

// Which beam was hit — used to run the grain along each bar's own axis.
float gBeam;

float map(vec3 p) {
  // Spin about the view axis. This is the ONLY rotation that preserves the
  // illusion, so the object can turn forever without ever resolving.
  vec3 d = normalize(vec3(1.0, 1.0, 1.0));
  p = rotAxis(p, d, uTime * 0.085);

  // Centre the figure: it spans [0,L] on each axis, centroid at L/2.
  vec3 q = p + vec3(L * 0.5);

  float b1 = sdBox(q - vec3(L * 0.5, 0.0, 0.0), vec3(L * 0.5 + TH, TH, TH));
  float b2 = sdBox(q - vec3(L, L * 0.5, 0.0), vec3(TH, L * 0.5 + TH, TH));
  float b3 = sdBox(q - vec3(L, L, L * 0.5), vec3(TH, TH, L * 0.5 + TH));

  float m = min(b1, min(b2, b3));
  gBeam = (m == b1) ? 0.0 : ((m == b2) ? 1.0 : 2.0);
  return m;
}

vec3 calcNormal(vec3 p) {
  vec2 e = vec2(0.0012, 0.0);
  return normalize(vec3(
    map(p + e.xyy) - map(p - e.xyy),
    map(p + e.yxy) - map(p - e.yxy),
    map(p + e.yyx) - map(p - e.yyx)
  ));
}

// Cheap ambient occlusion — this is most of what makes the joints read as
// solid rather than as flat overlapping rectangles.
float calcAO(vec3 p, vec3 n) {
  float occ = 0.0, sca = 1.0;
  for (int i = 0; i < 5; i++) {
    float h = 0.012 + 0.11 * float(i) / 4.0;
    float d = map(p + n * h);
    occ += (h - d) * sca;
    sca *= 0.82;
  }
  return clamp(1.0 - 2.2 * occ, 0.0, 1.0);
}

float softShadow(vec3 ro, vec3 rd) {
  float res = 1.0, t = 0.02;
  for (int i = 0; i < 22; i++) {
    float h = map(ro + rd * t);
    res = min(res, 9.0 * h / t);
    t += clamp(h, 0.015, 0.18);
    if (res < 0.004 || t > 3.2) break;
  }
  return clamp(res, 0.0, 1.0);
}

// --------------------------------------------------------------- material --
// Damascus strata, triplanar so the layers flow along each bar.
void damascus(vec3 p, vec3 n, out float strata, out float hardness, out vec2 grad) {
  vec3 an = abs(n);
  vec2 uvP = (an.x > an.y && an.x > an.z) ? p.yz
           : (an.y > an.z)                ? p.xz
                                          : p.xy;

  // Warp, then quantise. The quantisation is what reads as forged layers
  // rather than as smoke — it is the whole trick.
  vec2 w = vec2(fbm(uvP * 2.1), fbm(uvP * 2.1 + vec2(4.3, 1.7)));
  float field = uvP.y * 3.2 + 3.4 * w.x + 1.6 * fbm(uvP * 1.3 + w);

  float LAYERS = 15.0;
  float band = field * LAYERS;
  float f = fract(band);
  strata = smoothstep(0.0, 0.45, f) * smoothstep(1.0, 0.55, f);
  hardness = mix(0.6, 1.0, mod(floor(band), 2.0));

  float e = 0.01;
  float f2 = (uvP.y + e) * 3.2 + 3.4 * w.x + 1.6 * fbm((uvP + vec2(0.0, e)) * 1.3 + w);
  grad = vec2(0.0, (f2 - field) / e);
}

void main() {
  vec2 uv = (vUv - 0.5);
  float aspect = uResolution.x / max(uResolution.y, 1.0);
  uv.x *= aspect;

  // Place the object right-of-centre on wide screens so the headline has the
  // left. uFocus pulls it back to centre on narrow ones.
  // Right-of-centre on wide screens, centred when the copy stacks. The
  // multiplier is the zoom: SMALLER means the object fills MORE of the frame.
  // The WHOLE figure has to be in frame or the impossibility never registers -
  // a cropped Penrose triangle is just some bars.
  //
  // The copy is centred beneath it, so the object sits high and centred with
  // the scrim carrying it down into the text. Zoom is generous: the figure
  // should read as a struck emblem, not a background texture.
  // Right-of-centre on wide screens so it never sits under the headline;
  // centred only when the copy stacks below the lg breakpoint.
  // Right of the copy on wide screens; centred and higher when text stacks.
  // y is NEGATIVE to lift the figure - vUv.y runs bottom-up.
  vec2 centre = vec2(mix(0.38, 0.0, uFocus) * aspect, mix(-0.04, 0.20, uFocus));
  vec2 sp = (uv - centre) * mix(4.4, 5.0, uFocus);

  // ---- orthographic camera on the isometric axis ------------------------
  vec3 d = normalize(vec3(1.0, 1.0, 1.0));

  // The cursor tilts the camera a MAXIMUM of ~2.4 degrees. That is the whole
  // interaction: barely enough to crack the seam open, never enough to make
  // the object look broken.
  float tiltX = uPointer.x * 0.042;
  float tiltY = uPointer.y * 0.042;
  vec3 upGuess = vec3(0.0, 1.0, 0.0);
  vec3 right = normalize(cross(d, upGuess));
  vec3 up    = normalize(cross(right, d));
  vec3 viewDir = normalize(d + right * tiltX + up * tiltY);
  right = normalize(cross(viewDir, upGuess));
  up    = normalize(cross(right, viewDir));

  vec3 ro = viewDir * 7.0 + right * sp.x + up * sp.y;
  vec3 rd = -viewDir;

  // ---- march ------------------------------------------------------------
  float t = 0.0;
  float hit = 0.0;
  for (int i = 0; i < 84; i++) {
    vec3 p = ro + rd * t;
    float h = map(p);
    if (h < 0.0016) { hit = 1.0; break; }
    t += h;
    if (t > 14.0) break;
  }

  // ---- background: quiet, so the object owns the frame ------------------
  vec3 ink = vec3(0.035, 0.035, 0.043);
  vec3 col = ink;
  // A faint warm pool behind the object gives it somewhere to sit.
  col += vec3(0.788, 0.663, 0.380) * exp(-length(sp) * 0.70) * 0.055;

  if (hit > 0.5) {
    vec3 p = ro + rd * t;
    vec3 n = calcNormal(p);

    float strata, hardness;
    vec2 grad;
    damascus(p, n, strata, hardness, grad);

    // Grain tangent: perpendicular to the band gradient, projected onto the
    // surface. This is what makes the specular rake along the bar.
    vec3 tRef = abs(n.y) < 0.9 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0);
    vec3 tangent = normalize(cross(n, tRef));

    vec3 lightDir = normalize(vec3(0.55 + uPointer.x * 0.5, 0.85 + uPointer.y * 0.35, 0.75));
    vec3 V = -rd;
    vec3 H = normalize(lightDir + V);

    float dotTH = dot(tangent, H);
    float sinTH = sqrt(max(0.0, 1.0 - dotTH * dotTH));
    float aniso = pow(sinTH, 18.0) * hardness;

    float diff = max(0.0, dot(n, lightDir));
    float ao   = calcAO(p, n);
    float sh   = softShadow(p + n * 0.02, lightDir);
    float fres = pow(1.0 - max(0.0, dot(n, V)), 4.0);

    vec3 steel = vec3(0.215, 0.220, 0.250);
    vec3 pale  = vec3(0.56, 0.565, 0.60);
    vec3 ox  = vec3(0.788, 0.663, 0.380);
    vec3 giltHi= vec3(0.941, 0.886, 0.737);

    vec3 m = mix(steel, pale, strata * 0.70);
    m *= 0.45 + 0.55 * ao;                 // keep AO readable, not crushing
    m += pale * diff * (0.35 + 0.65 * sh) * 0.55;

    // Gold is LIGHT on the steel, never pigment.
    m += ox   * aniso * (0.4 + 0.6 * sh) * 2.5;
    m += giltHi * pow(aniso, 2.0) * (0.4 + 0.6 * sh) * 1.1;
    m += ox   * fres * 0.22;
    m += ox   * strata * diff * 0.16;
    // A cool rim keeps the silhouette legible against the dark ground.
    m += vec3(0.50, 0.55, 0.66) * pow(1.0 - max(0.0, dot(n, V)), 2.0) * 0.16;

    col = m;
  }

  // ---- vignette + fade into the page ------------------------------------
  col = mix(ink, col, smoothstep(1.30, 0.20, length(uv)) * 0.97);
  col = mix(ink, col, smoothstep(0.0, 0.30, vUv.y));

  // ---- entrance: the figure is struck out of the dark --------------------
  float wipe = smoothstep(uReveal - 0.40, uReveal + 0.06, vUv.y * 0.5 + 0.5);
  col = mix(col, ink, wipe);

  gl_FragColor = vec4(col, 1.0);
}
`;
