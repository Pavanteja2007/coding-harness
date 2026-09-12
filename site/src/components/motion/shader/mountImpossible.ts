/**
 * ogl lifecycle for the impossible-object hero.
 *
 * Raymarching is heavier than a noise field, so the DPR caps are lower here
 * than they would be for a flat shader: 1.5 on desktop, 0.7 on mobile. The
 * object is large, smooth and slow-moving, so it holds up at those densities.
 *
 * Ladder:
 *   desktop WebGL2          full march, cursor tilts the camera off-axis
 *   mobile / no hover       light and tilt auto-orbit; no pointer listener
 *   prefers-reduced-motion  one composed frame, perfectly on-axis, no RAF
 *   no WebGL                returns null; the caller keeps the static image
 *   tab hidden / offscreen  RAF cancelled
 */
import { Renderer, Program, Mesh, Triangle } from "ogl";
import { VERT, FRAG } from "./impossible.glsl";

export const FROZEN_TIME = 14.0;

export type ForgeHandle = { destroy: () => void };

function hasWebGL(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(
      c.getContext("webgl2") ||
      c.getContext("webgl") ||
      c.getContext("experimental-webgl")
    );
  } catch {
    return false;
  }
}

export function mountImpossible(
  host: HTMLElement,
  opts: { reducedMotion: boolean; mobile: boolean }
): ForgeHandle | null {
  if (!hasWebGL()) return null;

  let renderer: Renderer;
  try {
    renderer = new Renderer({
      alpha: false,
      antialias: false,
      depth: false,
      dpr: opts.mobile ? 0.7 : Math.min(window.devicePixelRatio || 1, 1.5),
      powerPreference: "high-performance",
    });
  } catch {
    return null;
  }

  const gl = renderer.gl;
  host.appendChild(gl.canvas);
  Object.assign(gl.canvas.style, {
    width: "100%",
    height: "100%",
    display: "block",
  });

  const program = new Program(gl, {
    vertex: VERT,
    fragment: FRAG,
    uniforms: {
      uTime: { value: FROZEN_TIME },
      uResolution: { value: [1, 1] },
      uPointer: { value: [0, 0] },
      uReveal: { value: opts.reducedMotion ? 1.6 : 0.0 },
      uFocus: { value: opts.mobile ? 1 : 0 },
    },
  });

  const mesh = new Mesh(gl, { geometry: new Triangle(gl), program });

  const resize = () => {
    const r = host.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    renderer.setSize(r.width, r.height);
    program.uniforms.uResolution.value = [r.width, r.height];
    // Below the lg breakpoint the copy stacks above, so centre the object.
    program.uniforms.uFocus.value = r.width < 1024 ? 1 : 0;
  };
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(host);

  // ---- reduced motion: perfectly on-axis, one frame ----------------------
  // The illusion is INTACT here. Freezing it off-axis would show a broken
  // object to exactly the people least able to interpret why.
  if (opts.reducedMotion) {
    program.uniforms.uTime.value = FROZEN_TIME;
    program.uniforms.uPointer.value = [0, 0];
    program.uniforms.uReveal.value = 1.6;
    renderer.render({ scene: mesh });
    return {
      destroy() {
        ro.disconnect();
        gl.canvas.remove();
      },
    };
  }

  let targetX = 0;
  let targetY = 0;
  let curX = 0;
  let curY = 0;

  const usePointer = window.matchMedia("(hover: hover)").matches;
  const onPointer = (e: PointerEvent) => {
    const r = host.getBoundingClientRect();
    targetX = ((e.clientX - r.left) / r.width) * 2 - 1;
    targetY = 1 - ((e.clientY - r.top) / r.height) * 2;
  };
  if (usePointer) {
    window.addEventListener("pointermove", onPointer, { passive: true });
  }

  let raf = 0;
  let tabVisible = !document.hidden;
  let onScreen = true;
  const t0 = performance.now();

  const draw = (now: number) => {
    raf = requestAnimationFrame(draw);
    const elapsed = (now - t0) / 1000;

    if (!usePointer) {
      // Touch: drift a little so the seam breathes open and shut on its own.
      targetX = Math.sin(elapsed * 0.23) * 0.55;
      targetY = Math.cos(elapsed * 0.17) * 0.35;
    }
    // Heavy lag. The illusion should feel like it resists being disturbed.
    curX += (targetX - curX) * 0.035;
    curY += (targetY - curY) * 0.035;

    program.uniforms.uPointer.value = [curX, curY];
    program.uniforms.uTime.value = elapsed;
    program.uniforms.uReveal.value = Math.min(1.6, elapsed * 0.95);
    renderer.render({ scene: mesh });
  };

  const start = () => {
    if (!raf && tabVisible && onScreen) raf = requestAnimationFrame(draw);
  };
  const stop = () => {
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  };

  const io = new IntersectionObserver(([entry]) => {
    onScreen = entry.isIntersecting;
    onScreen ? start() : stop();
  });
  io.observe(host);

  const onVis = () => {
    tabVisible = !document.hidden;
    tabVisible ? start() : stop();
  };
  document.addEventListener("visibilitychange", onVis);

  const onLost = (e: Event) => {
    e.preventDefault();
    stop();
    host.dataset.shaderLost = "1";
  };
  gl.canvas.addEventListener("webglcontextlost", onLost);

  start();

  return {
    destroy() {
      stop();
      io.disconnect();
      ro.disconnect();
      if (usePointer) window.removeEventListener("pointermove", onPointer);
      document.removeEventListener("visibilitychange", onVis);
      gl.canvas.removeEventListener("webglcontextlost", onLost);
      gl.canvas.remove();
    },
  };
}
