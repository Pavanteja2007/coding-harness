/**
 * ogl lifecycle for the infinite-descent fractal.
 *
 * This is the heaviest shader on the site — up to 140 march steps with a
 * 9-iteration fold at each one — so the DPR caps are conservative and there is
 * an adaptive quality step: if the first second of frames runs slow, the
 * render scale drops once and stays there. Better a slightly softer image at
 * 60fps than a crisp one at 25.
 *
 * Ladder:
 *   desktop WebGL2          full march, cursor steers the descent
 *   slow GPU                render scale drops once, automatically
 *   mobile                  reduced scale, descent auto-steers
 *   prefers-reduced-motion  one composed frame, no RAF at all
 *   no WebGL                returns null; the caller keeps the static image
 *   tab hidden / offscreen  RAF cancelled
 */
import { Renderer, Program, Mesh, Triangle } from "ogl";
import { VERT, FRAG } from "./fractal.glsl";

export const FROZEN_TIME = 31.5;

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

export function mountFractal(
  host: HTMLElement,
  opts: { reducedMotion: boolean; mobile: boolean }
): ForgeHandle | null {
  if (!hasWebGL()) return null;

  let scale = opts.mobile ? 0.55 : Math.min(window.devicePixelRatio || 1, 1.35);

  let renderer: Renderer;
  try {
    renderer = new Renderer({
      alpha: false,
      antialias: false,
      depth: false,
      dpr: scale,
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
      uReveal: { value: opts.reducedMotion ? 1 : 0 },
    },
  });

  const mesh = new Mesh(gl, { geometry: new Triangle(gl), program });

  const resize = () => {
    const r = host.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    renderer.setSize(r.width, r.height);
    program.uniforms.uResolution.value = [
      r.width * scale,
      r.height * scale,
    ];
  };
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(host);

  // ---- reduced motion: one frame, no loop --------------------------------
  if (opts.reducedMotion) {
    program.uniforms.uTime.value = FROZEN_TIME;
    program.uniforms.uReveal.value = 1;
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

  // Adaptive quality: sample the first ~90 frames, drop scale once if slow.
  let frames = 0;
  let slowFrames = 0;
  let downgraded = false;
  let last = t0;

  const draw = (now: number) => {
    raf = requestAnimationFrame(draw);
    const elapsed = (now - t0) / 1000;
    const dt = now - last;
    last = now;

    if (!downgraded && frames < 90) {
      frames += 1;
      if (dt > 26) slowFrames += 1;         // slower than ~38fps
      if (frames === 90 && slowFrames > 30) {
        downgraded = true;
        scale = Math.max(0.45, scale * 0.68);
        renderer.dpr = scale;
        resize();
      }
    }

    if (!usePointer) {
      targetX = Math.sin(elapsed * 0.11) * 0.7;
      targetY = Math.cos(elapsed * 0.08) * 0.5;
    }
    curX += (targetX - curX) * 0.028;
    curY += (targetY - curY) * 0.028;

    program.uniforms.uPointer.value = [curX, curY];
    program.uniforms.uTime.value = elapsed + FROZEN_TIME;
    program.uniforms.uReveal.value = Math.min(1, elapsed * 0.55);
    renderer.render({ scene: mesh });
  };

  const start = () => {
    if (!raf && tabVisible && onScreen) {
      last = performance.now();
      raf = requestAnimationFrame(draw);
    }
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
