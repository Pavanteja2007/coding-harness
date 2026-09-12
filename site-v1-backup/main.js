/* =========================================================================
   Vex landing page — main.js
   Vanilla JS. Animate transform/opacity only. Everything degrades to a
   correct static page with no JS and honors prefers-reduced-motion.
   ========================================================================= */
(function () {
  "use strict";

  var docEl = document.documentElement;
  docEl.classList.add("js");

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- Mobile nav -------------------------------------------------------- */
  var toggle = document.getElementById("navToggle");
  var menu = document.getElementById("navMenu");
  if (toggle && menu) {
    var closeMenu = function () {
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-label", "Open menu");
    };
    toggle.addEventListener("click", function () {
      var open = menu.hidden;
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    });
    menu.addEventListener("click", function (e) {
      if (e.target.closest("a")) closeMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !menu.hidden) { closeMenu(); toggle.focus(); }
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 820 && !menu.hidden) closeMenu();
    });
  }

  /* ---- Reveal on scroll -------------------------------------------------- */
  var reveals = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (reduceMotion || !("IntersectionObserver" in window)) {
    reveals.forEach(function (el) { el.classList.add("is-visible"); });
  } else {
    var revObs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revObs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    reveals.forEach(function (el) { revObs.observe(el); });
  }

  /* Small helper: run a callback once when an element scrolls into view. */
  function onEnter(el, cb, threshold) {
    if (!el) return;
    if (reduceMotion || !("IntersectionObserver" in window)) { cb(); return; }
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { cb(); obs.unobserve(entry.target); }
      });
    }, { threshold: threshold || 0.35 });
    obs.observe(el);
  }

  /* ---- Count-up stats ---------------------------------------------------- */
  document.querySelectorAll(".stat__num[data-count]").forEach(function (el) {
    var target = parseFloat(el.getAttribute("data-count"));
    var prefix = el.getAttribute("data-prefix") || "";
    var suffix = el.getAttribute("data-suffix") || "";
    var render = function (v) { el.textContent = prefix + v + suffix; };
    if (reduceMotion) { render(target); return; }
    onEnter(el, function () {
      var dur = 1100, start = null;
      function frame(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
        render(Math.round(eased * target));
        if (p < 1) requestAnimationFrame(frame);
        else render(target);
      }
      requestAnimationFrame(frame);
    }, 0.5);
  });

  /* ---- Hero terminal step animation -------------------------------------- */
  var term = document.getElementById("heroTerm");
  var termBody = document.getElementById("termBody");
  if (term && termBody) {
    var lines = Array.prototype.slice.call(termBody.querySelectorAll(".tl"));
    if (reduceMotion) {
      // Leave fully rendered.
    } else {
      term.classList.add("term--anim");
      onEnter(term, function () {
        lines.forEach(function (line, i) {
          setTimeout(function () { line.classList.add("is-in"); }, i * 420);
        });
      }, 0.3);
    }
  }

  /* ---- Loop: the verifier gate opening ----------------------------------- */
  var loop = document.getElementById("loop");
  if (loop) {
    var gateNode = loop.querySelector(".loop__node--gate");
    var gateArrow = loop.querySelector("[data-gatearrow]");
    var outNode = loop.querySelector(".loop__node--out");
    var gateMark = loop.querySelector("[data-gate]");
    var openGate = function () {
      if (gateNode) gateNode.classList.add("is-passed");
      if (gateMark) gateMark.textContent = "PASSED ✓";
      if (gateArrow) gateArrow.classList.add("is-open");
      if (outNode) outNode.classList.add("is-open");
    };
    if (reduceMotion) { openGate(); }
    else { onEnter(loop, function () { setTimeout(openGate, 900); }, 0.4); }
  }

  /* ---- Session board ----------------------------------------------------- */
  var board = document.getElementById("board");
  if (board) {
    var slots = {
      running: board.querySelector('[data-col="running"]'),
      verifying: board.querySelector('[data-col="verifying"]'),
      passed: board.querySelector('[data-col="passed"]')
    };
    // 12 representative rows; 8 are killed-and-resumed.
    var rows = [];
    for (var i = 1; i <= 12; i++) {
      rows.push({
        id: "task-" + (i < 10 ? "0" + i : i),
        route: (i % 3 === 0) ? "hard" : "cheap",
        resumed: i <= 8
      });
    }
    function rowEl(r, resumedVisible) {
      var el = document.createElement("div");
      el.className = "brow";
      var badges = '<span class="rbadge rbadge--' + r.route + '">' + r.route + '</span>';
      if (r.resumed && resumedVisible) {
        badges += ' <span class="rbadge rbadge--resume">resumed ✓</span>';
      }
      el.innerHTML = '<span class="brow__id">' + r.id + '</span><span>' + badges + '</span>';
      return el;
    }
    function place(col, r, resumedVisible) {
      if (slots[col]) slots[col].appendChild(rowEl(r, resumedVisible));
    }
    if (reduceMotion) {
      // Final state: everything passed, resume badges shown.
      rows.forEach(function (r) { place("passed", r, true); });
    } else {
      onEnter(board, function () {
        // Seed: some running, some verifying.
        rows.forEach(function (r, idx) {
          if (idx < 5) place("running", r, false);
          else if (idx < 8) place("verifying", r, false);
          else place("passed", r, false);
        });
        // Then sweep the 8 killed tasks into Passed, one by one.
        var toMove = rows.slice(0, 8);
        toMove.forEach(function (r, k) {
          setTimeout(function () {
            // Clear and re-render all columns in the advanced state.
            if (k === 0) {
              slots.running.innerHTML = "";
              slots.verifying.innerHTML = "";
            }
          }, 300);
          setTimeout(function () {
            place("passed", r, true);
            // Refill running/verifying with later, still-in-flight tasks.
            if (k === toMove.length - 1) {
              rows.slice(8).forEach(function (rr) { /* already in passed */ });
            }
          }, 500 + k * 260);
        });
      }, 0.35);
    }
  }

  /* ---- Copy buttons ------------------------------------------------------ */
  function flash(btn, ok) {
    var label = btn.querySelector(".cmd__copy-label");
    var original = label ? label.textContent : "";
    if (label) label.textContent = ok ? "Copied" : "Press ⌘C";
    btn.classList.add("is-copied");
    btn.setAttribute("aria-live", "polite");
    setTimeout(function () {
      if (label) label.textContent = original || "Copy";
      btn.classList.remove("is-copied");
    }, 1600);
  }
  document.querySelectorAll(".cmd").forEach(function (cmd) {
    var btn = cmd.querySelector(".cmd__copy");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var text = cmd.getAttribute("data-copy") ||
        (cmd.querySelector(".cmd__text") ? cmd.querySelector(".cmd__text").textContent : "");
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { flash(btn, true); },
          function () { flash(btn, false); }
        );
      } else {
        try {
          var ta = document.createElement("textarea");
          ta.value = text; ta.setAttribute("readonly", "");
          ta.style.position = "absolute"; ta.style.left = "-9999px";
          document.body.appendChild(ta); ta.select();
          document.execCommand("copy"); document.body.removeChild(ta);
          flash(btn, true);
        } catch (e) { flash(btn, false); }
      }
    });
  });

  /* ---- Tabs (WAI-ARIA) --------------------------------------------------- */
  var tablist = document.querySelector(".tabs__list");
  if (tablist) {
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
    function selectTab(tab) {
      tabs.forEach(function (t) {
        var selected = t === tab;
        t.setAttribute("aria-selected", String(selected));
        t.tabIndex = selected ? 0 : -1;
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !selected;
      });
    }
    tabs.forEach(function (tab, idx) {
      tab.addEventListener("click", function () { selectTab(tab); });
      tab.addEventListener("keydown", function (e) {
        var i = null;
        if (e.key === "ArrowRight") i = (idx + 1) % tabs.length;
        else if (e.key === "ArrowLeft") i = (idx - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") i = 0;
        else if (e.key === "End") i = tabs.length - 1;
        if (i !== null) { e.preventDefault(); tabs[i].focus(); selectTab(tabs[i]); }
      });
    });
  }

  /* ---- Nav: subtle solidify on scroll ------------------------------------ */
  var nav = document.getElementById("nav");
  if (nav) {
    var onScroll = function () {
      if (window.scrollY > 8) nav.style.borderBottomColor = "var(--hairline)";
    };
    onScroll();
  }
})();
