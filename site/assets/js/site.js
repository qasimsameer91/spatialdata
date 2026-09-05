/* ============================================================================
   spatialdata — site motion.
   Everything is IntersectionObserver + transform/opacity so it stays on the
   compositor; no scroll handlers doing layout work, no animation library.
   ========================================================================= */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ------------------------------------------------------------ nav ----- */
  function nav() {
    var bar = $(".nav");
    if (!bar) return;
    var onScroll = function () { bar.classList.toggle("stuck", window.scrollY > 12); };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });

    var burger = $(".nav-burger"), links = $(".nav-links");
    if (burger && links) {
      burger.addEventListener("click", function () {
        var open = links.classList.toggle("open");
        burger.textContent = open ? "×" : "≡";
        burger.setAttribute("aria-expanded", String(open));
      });
      $$(".nav-links a").forEach(function (a) {
        a.addEventListener("click", function () {
          links.classList.remove("open");
          burger.textContent = "≡";
        });
      });
    }

    // Mark the current page without hard-coding a class into every file.
    var here = location.pathname.split("/").pop() || "index.html";
    $$(".nav-links a").forEach(function (a) {
      if ((a.getAttribute("href") || "").split("/").pop() === here) a.classList.add("active");
    });
  }

  /* -------------------------------------------------------- reveals ----- */
  function reveals() {
    var items = $$("[data-rise],[data-scale],.reveal-line");
    if (!items.length) return;

    if (reduced || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }

    // Stagger siblings that share a parent so rows animate in sequence.
    var seen = new Map();
    items.forEach(function (el) {
      var p = el.parentElement;
      var i = seen.get(p) || 0;
      if (!el.style.getPropertyValue("--i")) el.style.setProperty("--i", i);
      seen.set(p, i + 1);
    });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("in");
        io.unobserve(e.target);          // one-shot: no flicker on scroll-back
      });
    }, { rootMargin: "0px 0px -9% 0px", threshold: 0.12 });

    items.forEach(function (el) { io.observe(el); });
  }

  /* ------------------------------------------------------- parallax ----- */
  function parallax() {
    var media = $(".hero-media");
    if (!media || reduced) return;
    var ticking = false;
    var apply = function () {
      // Cheap: one custom property, composited transform, no layout read
      // beyond scrollY.
      media.style.setProperty("--py", (window.scrollY * 0.28).toFixed(1) + "px");
      ticking = false;
    };
    window.addEventListener("scroll", function () {
      if (!ticking) { ticking = true; requestAnimationFrame(apply); }
    }, { passive: true });
    apply();
  }

  /* --------------------------------------------------------- counters --- */
  function counters() {
    var nodes = $$("[data-count]");
    if (!nodes.length) return;
    if (reduced || !("IntersectionObserver" in window)) {
      nodes.forEach(function (n) { n.textContent = n.dataset.count + (n.dataset.suffix || ""); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target, end = parseFloat(el.dataset.count);
        var dec = (el.dataset.count.split(".")[1] || "").length;
        var suffix = el.dataset.suffix || "", t0 = performance.now(), dur = 1250;
        (function tick(now) {
          var p = Math.min(1, (now - t0) / dur);
          var eased = 1 - Math.pow(1 - p, 3);
          el.textContent = (end * eased).toFixed(dec) + suffix;
          if (p < 1) requestAnimationFrame(tick);
        })(t0);
        io.unobserve(el);
      });
    }, { threshold: 0.5 });
    nodes.forEach(function (n) { io.observe(n); });
  }

  /* ------------------------------------------------------ card sheen ---- */
  function sheen() {
    if (reduced || matchMedia("(hover: none)").matches) return;
    $$(".card").forEach(function (c) {
      c.addEventListener("pointermove", function (e) {
        var r = c.getBoundingClientRect();
        c.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100) + "%");
        c.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100) + "%");
      });
    });
  }

  /* ---------------------------------------------------- hero headline --- */
  function heroLines() {
    var h = $("[data-lines]");
    if (!h) return;
    // Wrap each line in the mask/inner pair the CSS animates.
    var parts = h.innerHTML.split("<br>");
    h.innerHTML = parts.map(function (p, i) {
      return '<span class="reveal-line" style="--i:' + i + '"><span>' + p.trim() + "</span></span>";
    }).join("");
    requestAnimationFrame(function () {
      $$(".reveal-line", h).forEach(function (l) { l.classList.add("in"); });
    });
  }

  /* ----------------------------------------------------- hero video ----- */
  function heroVideo() {
    var v = $(".hero-media video");
    if (!v) return;
    if (reduced) { v.removeAttribute("autoplay"); v.pause(); return; }
    // Some browsers refuse autoplay until a gesture; the poster covers it.
    var p = v.play();
    if (p && p.catch) p.catch(function () { /* poster stays visible */ });
    // Don't burn CPU on a tab nobody is looking at.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) v.pause(); else v.play().catch(function () {});
    });
  }

  /* -------------------------------------------------------- checkout ---- */
  /* No payment processor is wired up. Each buy button carries data-plan;
     point CHECKOUT_URLS at Stripe/Paddle/Lemon Squeezy links and it works. */
  var CHECKOUT_URLS = {
    solo: "",
    studio: "",
    enterprise: ""
  };

  function checkout() {
    $$("[data-plan]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        var plan = btn.dataset.plan;
        var url = CHECKOUT_URLS[plan];
        if (url) { location.href = url; return; }
        e.preventDefault();
        var note = $("#checkout-note");
        if (note) {
          note.textContent =
            'No checkout link is configured for the "' + plan +
            '" plan yet — set CHECKOUT_URLS in site/assets/js/site.js.';
          note.hidden = false;
          note.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
        }
      });
    });
  }

  /* ------------------------------------------------------------ year ---- */
  function year() {
    $$("[data-year]").forEach(function (n) { n.textContent = new Date().getFullYear(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    nav(); heroLines(); reveals(); parallax();
    counters(); sheen(); heroVideo(); checkout(); year();
  });
})();
