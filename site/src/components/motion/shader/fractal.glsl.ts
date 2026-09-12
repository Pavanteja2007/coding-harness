/**
 * INFINITE DESCENT — raymarched Apollonian gasket.
 *
 * An Apollonian packing is the fractal you get by repeatedly inverting space
 * through spheres: every gap between spheres contains smaller spheres, and
 * every gap between those contains smaller ones again, without end. The
 * structure is genuinely infinite — not a tiled texture, not a loop. Zooming
 * in forever reveals new architecture at every scale because the geometry is
 * evaluated per-pixel from the fold equation.
 *
 * Why it suits this product: the fractal is exact. It is defined by an
 * iterated function system where each step is a fold and a sphere inversion,
 * and it holds together at every magnification. That is the opposite of the
 * impossible object — this one survives arbitrary inspection.
 *
 * The kernel is Iñigo Quílez's Apollonian formulation:
 *     p = -1 + 2·fract(0.5p + 0.5)     fold into the unit cell
 *     p *= s / dot(p,p)                 invert through a sphere
 * accumulating the scale factor so the distance estimate stays correct, and
 * capturing orbit traps on the way through for colour.
 *
 * Camera falls forward continuously. Because the fractal is self-similar under
 * the fold, translating by exactly one cell returns an identical field — so
 * the descent loops seamlessly and can run forever.
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
uniform vec2  uPointer;
uniform float uReveal;

varying vec2 vUv;

// Orbit trap, carried out of the fold loop for colouring.
vec4 gTrap;

/**
 * Apollonian distance estimator.
 *
 * The inversion radius s is animated slowly, which breathes the whole packing
 * — spheres swell and the gaps between them open and close, which is what
 * stops an infinite zoom from feeling static.
 */
float apollonian(vec3 p) {
  float s = 1.18 + 0.11 * sin(uTime * 0.043);
  float scale = 1.0;
  vec4 orb = vec4(1e4);

  for (int i = 0; i < 9; i++) {
    // Fold space into the unit cell. This is what makes it infinite.
    p = -1.0 + 2.0 * fract(0.5 * p + 0.5);

    float r2 = dot(p, p);
    // Orbit traps: how close the orbit came to each axis and to the origin.
    orb = min(orb, vec4(abs(p), r2));

    float k = s / r2;   // sphere inversion
    p *= k;
    scale *= k;
  }

  gTrap = orb;
  // Distance to the plane, corrected by the accumulated scale.
  return 0.25 * abs(p.y) / scale;
}

float map(vec3 p) {
  return apollonian(p);
}

vec3 calcNormal(vec3 p, float t) {
  // Epsilon scales with distance or the normal goes to noise deep in the zoom.
  vec2 e = vec2(1.0, -1.0) * 0.5773 * 0.0008 * t;
  return normalize(
    e.xyy * map(p + e.xyy) +
    e.yyx * map(p + e.yyx) +
    e.yxy * map(p + e.yxy) +
    e.xxx * map(p + e.xxx)
  );
}

float softShadow(vec3 ro, vec3 rd, float tmax) {
  float res = 1.0;
  float t = 0.004;
  for (int i = 0; i < 26; i++) {
    float h = map(ro + rd * t);
    res = min(res, 12.0 * h / t);
    t += clamp(h, 0.002, 0.06);
    if (res < 0.002 || t > tmax) break;
  }
  return clamp(res, 0.0, 1.0);
}

float calcAO(vec3 p, vec3 n) {
  float occ = 0.0;
  float sca = 1.0;
  for (int i = 0; i < 5; i++) {
    float h = 0.005 + 0.03 * float(i);
    float d = map(p + n * h);
    occ += (h - d) * sca;
    sca *= 0.85;
  }
  return clamp(1.0 - 3.0 * occ, 0.0, 1.0);
}

void main() {
  vec2 q = vUv;
  vec2 uv = (q - 0.5);
  float aspect = uResolution.x / max(uResolution.y, 1.0);
  uv.x *= aspect;

  // ---- the descent -------------------------------------------------------
  // Falling forward at a constant rate. One unit is one cell, so the field is
  // identical each time we pass through — the fall is seamless and endless.
  float fall = uTime * 0.085;

  // Cursor steers the descent, gently and with heavy lag applied host-side.
  vec2 steer = uPointer * 0.30;

  vec3 ro = vec3(
    0.62 + steer.x * 0.5 + 0.16 * sin(uTime * 0.037),
    0.34 + steer.y * 0.42 + 0.13 * cos(uTime * 0.029),
    fall
  );

  // Look slightly inward so the walls sweep past rather than rushing head-on.
  vec3 ta = vec3(steer.x * 0.22, steer.y * 0.18, fall + 1.0);
  vec3 ww = normalize(ta - ro);
  vec3 uu = normalize(cross(vec3(0.0, 1.0, 0.0), ww));
  vec3 vv = normalize(cross(ww, uu));
  // Slow roll: the horizon turns, which is most of why a descent feels like
  // falling rather than like moving.
  float roll = uTime * 0.021;
  vec3 rr = uu * cos(roll) + vv * sin(roll);
  vec3 rv = -uu * sin(roll) + vv * cos(roll);

  vec3 rd = normalize(uv.x * rr + uv.y * rv + 1.45 * ww);

  // ---- march -------------------------------------------------------------
  float t = 0.0;
  float hit = 0.0;
  vec4 trap = vec4(0.0);

  for (int i = 0; i < 140; i++) {
    vec3 p = ro + rd * t;
    float h = map(p);
    if (h < 0.00035 * t) { hit = 1.0; trap = gTrap; break; }
    t += h * 0.86;           // slight understep: the DE is not exact
    if (t > 4.2) break;
  }

  // ---- palette -----------------------------------------------------------
  vec3 ink     = vec3(0.035, 0.034, 0.042);
  vec3 gilt    = vec3(0.788, 0.663, 0.380);
  vec3 giltHot = vec3(0.941, 0.886, 0.737);
  vec3 deep    = vec3(0.145, 0.110, 0.052);

  vec3 col = ink;

  if (hit > 0.5) {
    vec3 p = ro + rd * t;
    vec3 n = calcNormal(p, t);

    // Colour from the orbit traps. Different traps pick out different parts
    // of the structure, so the surface is varied without any texture lookup.
    vec3 mat = mix(deep, gilt, clamp(trap.y * 2.4, 0.0, 1.0));
    mat = mix(mat, giltHot, clamp(pow(trap.z, 2.0) * 1.8, 0.0, 1.0) * 0.55);
    mat = mix(mat, vec3(0.32, 0.30, 0.34), clamp(trap.x * 1.4, 0.0, 1.0) * 0.42);
    // A cool inner tone keeps it from being monochrome gold.
    mat = mix(mat, vec3(0.10, 0.13, 0.17), clamp(trap.w * 1.1, 0.0, 1.0) * 0.5);

    vec3 lig = normalize(vec3(0.6, 0.75, -0.45));
    float dif = clamp(dot(n, lig), 0.0, 1.0);
    float bac = clamp(dot(n, -lig), 0.0, 1.0) * 0.35;
    float amb = 0.45 + 0.55 * n.y;
    float ao  = calcAO(p, n);
    float sh  = softShadow(p + n * 0.002, lig, 0.6);
    float fre = pow(clamp(1.0 + dot(n, rd), 0.0, 1.0), 3.0);

    vec3 lin = vec3(0.0);
    lin += 2.35 * dif * vec3(1.00, 0.92, 0.76) * (0.32 + 0.68 * sh);
    lin += 0.70 * amb * vec3(0.46, 0.49, 0.60) * ao;
    lin += 0.32 * bac * vec3(0.55, 0.45, 0.30) * ao;
    lin += 0.95 * fre * giltHot * ao;

    col = mat * lin;

    // Specular sheen along the sphere surfaces.
    vec3 hal = normalize(lig - rd);
    float spe = pow(clamp(dot(n, hal), 0.0, 1.0), 42.0);
    col += spe * giltHot * 0.95 * sh;

    // Depth haze — this is what creates the sense of infinite distance.
    col = mix(col, ink, 1.0 - exp(-0.22 * t * t));
  } else {
    // Missed rays still get a faint warm glow toward the centre so the
    // cathedral reads as lit from within rather than floating in void.
    col = ink + gilt * 0.055 * exp(-length(uv) * 1.3);
  }

  // ---- grade -------------------------------------------------------------
  col = pow(clamp(col, 0.0, 1.0), vec3(0.74));           // lift midtones
  col *= 1.0 - 0.26 * dot(uv, uv);                        // vignette
  col = mix(col, ink, smoothstep(0.78, 1.25, length(uv))); // edges to ground

  // Entrance: the structure resolves out of black.
  col *= smoothstep(0.0, 0.85, uReveal);

  gl_FragColor = vec4(col, 1.0);
}
`;
