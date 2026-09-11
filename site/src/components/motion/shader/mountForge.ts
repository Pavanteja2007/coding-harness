/**
 * ogl lifecycle for the hero shader, with the whole degradation ladder.
 *
 * DESIGN.md §3.5:
 *   desktop WebGL2        full shader, DPR capped at 1.5, 60fps
 *   mobile / low-power    half-resolution render target, DPR cap 1.0
 *   prefers-reduced-motion FREEZE at a composed still frame (not removed)
 *   no WebGL / context lost static pre-rendered WebP underneath
 *   tab hidden            cancelAnimationFrame
 *   scrolled past hero    pause the RAF loop
 *
 * Returns null when WebGL is unavailable, so the caller simply leaves the
 * static fallback image showing - that rung needs no extra branch.
 */
import { Renderer, Program, Mesh, Triangle } from "ogl";
import { VERT, FRAG } from "./forge.glsl";

/** The composed frame the shader freezes on under reduced-motion. */
export const FROZEN_TIME = 18.0;

export type ForgeHandle = { destroy: () => void };

/**
 * Probe for WebGL support BEFORE constructing ogl's Renderer.
 *
 * ogl does not throw when a context cannot be created - it console.errors
 * ("unable to create webgl context") and returns a renderer with a null gl.
 * A try/catch therefore never fires, and a visitor on a WebGL-less machine
 * gets console errors instead of a clean static fallback. Probing first means
 * that rung degrades silently, which is what DESIGN.md §3.5 asks for.
 */
function hasWebGL(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return !!(
      canvas.getContext("webgl2") ||
      canvas.getContext("webgl") ||
      canvas.getContext("experimental-webgl")
    );
  } catch {
    return false;
  }
}

export function mountForge(
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
      // DESIGN.md §3.5. Mobile renders at half resolution and is upscaled by
      // CSS, which is why the cap can go below 1.
      dpr: opts.mobile ? 0.5 : Math.min(window.devicePixelRatio || 1, 1.5),
      powerPreference: "high-performance",
    });
  } catch {
    return null; // no WebGL - caller keeps the static image
  }

  const gl = renderer.gl;
  host.appendChild(gl.canvas);
  gl.canvas.style.width = "100%";
  gl.canvas.style.height = "100%";
  gl.canvas.style.display = "block";

  const program = new Program(gl, {
    vertex: VERT,
    fragment: FRAG,
    uniforms: {
      uTime: { value: FROZEN_TIME },
      uResolution: { value: [1, 1] },
    },
  });

  const mesh = new Mesh(gl, {
    geometry: new Triangle(gl),
    program,
  });

  const resize = () => {
    const rect = host.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    renderer.setSize(rect.width, rect.height);
    program.uniforms.uResolution.value = [rect.width, rect.height];
  };
  resize();

  const ro = new ResizeObserver(resize);
  ro.observe(host);

  // ---- reduced motion: render exactly one composed frame, then stop. ----
  if (opts.reducedMotion) {
    program.uniforms.uTime.value = FROZEN_TIME;
    renderer.render({ scene: mesh });
    return {
      destroy() {
        ro.disconnect();
        gl.canvas.remove();
      },
    };
  }

  let raf = 0;
  let tabVisible = !document.hidden;
  let onScreen = true;

  const draw = (t: number) => {
    raf = requestAnimationFrame(draw);
    // Scaled well down: a full cycle takes ~40-60s (DESIGN.md §3.4.1).
    program.uniforms.uTime.value = t * 0.001;
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

  const io = new IntersectionObserver(
    ([entry]) => {
      onScreen = entry.isIntersecting;
      onScreen ? start() : stop();
    },
    { rootMargin: "0px" }
  );
  io.observe(host);

  const onVisibility = () => {
    tabVisible = !document.hidden;
    tabVisible ? start() : stop();
  };
  document.addEventListener("visibilitychange", onVisibility);

  // A lost context must degrade to the static image, not a black rectangle.
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
      document.removeEventListener("visibilitychange", onVisibility);
      gl.canvas.removeEventListener("webglcontextlost", onLost);
      gl.canvas.remove();
    },
  };
}
