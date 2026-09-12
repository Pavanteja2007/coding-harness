"use client";

/**
 * THE VERIFICATION FIELD — the hero.
 *
 * What it is: a live graph of nodes and edges, drifting. Every few seconds a
 * verification wave expands from a random origin. Nodes it touches flip from
 * oxblood (unverified) to bone (proven) and hold, then decay back as the
 * guarantee goes stale and the next wave comes round.
 *
 * Why this and not a noise shader: the product's two real mechanisms are a
 * code graph and a verifier gate. This is those two things, moving. The wave
 * is not decoration — it is the gate sweeping the graph, and the decay is why
 * verification has to be continuous rather than a one-off claim.
 *
 * Canvas 2D rather than WebGL, deliberately: a few hundred nodes with additive
 * strokes is trivially cheap here, it antialiases better than a raymarch, and
 * it has no shader-compile or context-loss failure modes to ladder around.
 *
 * Interaction: the cursor displaces nodes it passes near, and the field
 * settles back. Edges are recomputed every frame from live positions, so the
 * graph genuinely re-wires as it moves rather than animating a fixed mesh.
 */
import { useEffect, useRef } from "react";

type Node = {
  x: number; y: number;
  vx: number; vy: number;
  hx: number; hy: number;   // home position, for the settle-back force
  r: number;
  /** 0 = unverified (oxblood), 1 = just verified (bone). Decays over time. */
  v: number;
};

const OX = [140, 27, 51] as const;        // #8C1B33
const OX_BRIGHT = [212, 96, 122] as const; // #D4607A
const BONE = [242, 239, 234] as const;     // #F2EFEA
const VERDANT = [95, 207, 160] as const;   // #5FCFA0

function lerp(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

export default function VerificationField({
  className = "",
}: {
  className?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) return;

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let w = 0;
    let h = 0;
    let dpr = 1;
    let nodes: Node[] = [];
    let raf = 0;
    let running = true;

    // Wave state: origin, radius, and whether one is currently travelling.
    let waveX = 0;
    let waveY = 0;
    let waveR = 0;
    let waveActive = false;
    let nextWaveAt = 0;

    const pointer = { x: -9999, y: -9999 };

    const build = () => {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width;
      h = rect.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Density scales with area so a phone is not overloaded and a wide
      // desktop does not look sparse.
      const target = Math.round(Math.min(210, Math.max(70, (w * h) / 9000)));
      nodes = Array.from({ length: target }, () => {
        const x = Math.random() * w;
        const y = Math.random() * h;
        return {
          x, y, hx: x, hy: y,
          vx: (Math.random() - 0.5) * 0.16,
          vy: (Math.random() - 0.5) * 0.16,
          // A few nodes are noticeably larger: hubs, like a real call graph.
          r: Math.random() < 0.12 ? 2.4 + Math.random() * 1.8 : 0.9 + Math.random() * 1.1,
          v: 0,
        };
      });
    };

    build();

    const onResize = () => build();
    window.addEventListener("resize", onResize);

    const onPointer = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      pointer.x = e.clientX - rect.left;
      pointer.y = e.clientY - rect.top;
    };
    const onLeave = () => {
      pointer.x = -9999;
      pointer.y = -9999;
    };
    const hoverCapable = window.matchMedia("(hover: hover)").matches;
    if (hoverCapable) {
      window.addEventListener("pointermove", onPointer, { passive: true });
      window.addEventListener("pointerleave", onLeave);
    }

    const LINK_DIST = 132;
    const LINK_DIST_SQ = LINK_DIST * LINK_DIST;

    const draw = (now: number) => {
      if (!running) return;
      raf = requestAnimationFrame(draw);

      // ---- wave scheduling -------------------------------------------------
      if (!waveActive && now > nextWaveAt) {
        waveActive = true;
        waveR = 0;
        // Waves start from a hub where possible: verification propagates out
        // from something already known-good.
        const hub = nodes.filter((n) => n.r > 2.4);
        const seed = hub.length
          ? hub[(Math.random() * hub.length) | 0]
          : nodes[(Math.random() * nodes.length) | 0];
        waveX = seed ? seed.x : w / 2;
        waveY = seed ? seed.y : h / 2;
      }
      if (waveActive) {
        waveR += Math.max(w, h) / 165;
        if (waveR > Math.hypot(w, h) * 1.1) {
          waveActive = false;
          nextWaveAt = now + 2600 + Math.random() * 2200;
        }
      }

      // ---- background ------------------------------------------------------
      ctx.fillStyle = "#0A0708";
      ctx.fillRect(0, 0, w, h);

      // ---- integrate -------------------------------------------------------
      for (const n of nodes) {
        n.x += n.vx;
        n.y += n.vy;

        // Settle back toward home, so the field never drifts apart.
        n.vx += (n.hx - n.x) * 0.00042;
        n.vy += (n.hy - n.y) * 0.00042;

        // Cursor displacement: push away, with a soft falloff.
        const dx = n.x - pointer.x;
        const dy = n.y - pointer.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 26000 && d2 > 0.01) {
          const f = (1 - d2 / 26000) * 0.42;
          const d = Math.sqrt(d2);
          n.vx += (dx / d) * f;
          n.vy += (dy / d) * f;
        }

        n.vx *= 0.982;
        n.vy *= 0.982;

        // Verification: the wave front is a ring, not a disc, so a node is
        // caught as the front passes it rather than staying lit forever.
        if (waveActive) {
          const dist = Math.hypot(n.x - waveX, n.y - waveY);
          if (Math.abs(dist - waveR) < 46) n.v = 1;
        }
        // Decay: a guarantee goes stale. This is the point of the piece.
        n.v *= 0.988;
      }

      // ---- edges -----------------------------------------------------------
      // Recomputed every frame from live positions, so the graph genuinely
      // re-wires as nodes move.
      ctx.lineWidth = 1;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK_DIST_SQ) continue;

          const t = 1 - d2 / LINK_DIST_SQ;
          const verified = Math.max(a.v, b.v);
          // Edge colour runs oxblood -> verdant as either end is verified.
          const r = lerp(OX[0], VERDANT[0], verified);
          const g = lerp(OX[1], VERDANT[1], verified);
          const bl = lerp(OX[2], VERDANT[2], verified);
          ctx.strokeStyle = `rgba(${r | 0}, ${g | 0}, ${bl | 0}, ${(t * 0.30 + verified * 0.30).toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // ---- the wave front --------------------------------------------------
      if (waveActive) {
        const fade = 1 - waveR / (Math.hypot(w, h) * 1.1);
        ctx.strokeStyle = `rgba(95, 207, 160, ${(fade * 0.20).toFixed(3)})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(waveX, waveY, waveR, 0, Math.PI * 2);
        ctx.stroke();
      }

      // ---- nodes -----------------------------------------------------------
      for (const n of nodes) {
        const base = n.r > 2.4 ? OX_BRIGHT : OX;
        const r = lerp(base[0], BONE[0], n.v);
        const g = lerp(base[1], BONE[1], n.v);
        const bl = lerp(base[2], BONE[2], n.v);
        const alpha = 0.42 + n.v * 0.58 + (n.r > 2.4 ? 0.18 : 0);

        ctx.fillStyle = `rgba(${r | 0}, ${g | 0}, ${bl | 0}, ${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + n.v * 1.1, 0, Math.PI * 2);
        ctx.fill();

        // Verified nodes get a halo while the guarantee is fresh.
        if (n.v > 0.08) {
          ctx.strokeStyle = `rgba(95, 207, 160, ${(n.v * 0.42).toFixed(3)})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.r + 5 + n.v * 5, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
    };

    if (reduced) {
      // Compose one still frame with a wave mid-flight, then stop. The image
      // still reads as a graph being verified; it simply does not move.
      waveActive = true;
      waveR = Math.min(w, h) * 0.42;
      waveX = w * 0.5;
      waveY = h * 0.42;
      for (let i = 0; i < 90; i++) draw(performance.now());
      running = false;
      cancelAnimationFrame(raf);
    } else {
      nextWaveAt = performance.now() + 600;
      raf = requestAnimationFrame(draw);
    }

    // Pause offscreen and when the tab is hidden.
    const io = new IntersectionObserver(([e]) => {
      if (reduced) return;
      if (e.isIntersecting && !running) {
        running = true;
        raf = requestAnimationFrame(draw);
      } else if (!e.isIntersecting && running) {
        running = false;
        cancelAnimationFrame(raf);
      }
    });
    io.observe(canvas);

    const onVis = () => {
      if (reduced) return;
      if (document.hidden && running) {
        running = false;
        cancelAnimationFrame(raf);
      } else if (!document.hidden && !running) {
        running = true;
        raf = requestAnimationFrame(draw);
      }
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      io.disconnect();
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVis);
      if (hoverCapable) {
        window.removeEventListener("pointermove", onPointer);
        window.removeEventListener("pointerleave", onLeave);
      }
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`absolute inset-0 h-full w-full ${className}`}
    />
  );
}
