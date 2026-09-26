/* Interface polish: header state on scroll, hero parallax, image fade-in,
   scroll reveal and the account menu. Everything respects reduced motion. */
(function () {
  "use strict";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var header = document.querySelector(".site-header");
  var hero = document.querySelector("[data-hero]");
  var media = document.querySelector("[data-hero-media]");
  var title = document.querySelector("[data-hero-title]");
  var img = document.querySelector("[data-hero-img]");

  /* Hero image: fade in once decoded, never pop. */
  if (img) {
    var shown = function () { img.classList.add("is-loaded"); };
    if (img.complete && img.naturalWidth) { requestAnimationFrame(shown); }
    else { img.addEventListener("load", shown, { once: true }); img.addEventListener("error", shown, { once: true }); }
  }

  /* Footer scene: same decode fade as the hero (lazy-loaded). */
  var fimg = document.querySelector("[data-footer-img]");
  if (fimg) {
    var fshow = function () { fimg.classList.add("is-loaded"); };
    if (fimg.complete && fimg.naturalWidth) { fshow(); }
    else { fimg.addEventListener("load", fshow, { once: true }); fimg.addEventListener("error", fshow, { once: true }); }
  }

  /* Scroll: header frosting + gentle hero parallax, batched per frame. */
  var ticking = false;
  function onScroll() {
    var y = window.scrollY || 0;
    if (header) header.classList.toggle("is-scrolled", y > 8);
    if (hero && !reduce) {
      var h = hero.offsetHeight || 1;
      if (y < h + 200) {
        var p = Math.min(y / h, 1);
        if (media) media.style.transform = "translate3d(0," + (y * 0.28).toFixed(1) + "px,0)";
        if (title) {
          title.style.transform = "translate3d(0," + (y * 0.12).toFixed(1) + "px,0)";
          title.style.opacity = String(Math.max(0, 1 - p * 1.6).toFixed(3));
        }
      }
    }
    ticking = false;
  }
  window.addEventListener("scroll", function () {
    if (!ticking) { ticking = true; requestAnimationFrame(onScroll); }
  }, { passive: true });
  onScroll();

  /* Reveal groups: children fade up as they enter, lightly staggered. */
  var groups = document.querySelectorAll("[data-reveal-group]");
  if (groups.length && "IntersectionObserver" in window && !reduce) {
    document.documentElement.classList.add("js-reveal");
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target, i = Array.prototype.indexOf.call(el.parentNode.children, el);
        el.style.transitionDelay = Math.min(i % 3, 2) * 70 + "ms";
        el.classList.add("is-in");
        io.unobserve(el);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    groups.forEach(function (g) {
      Array.prototype.forEach.call(g.children, function (c) {
        // Anything already on screen shows immediately; only below-the-fold items animate.
        if (c.getBoundingClientRect().top < window.innerHeight * 0.92) { c.classList.add("is-in"); }
        else { io.observe(c); }
      });
    });
    // Safety net: never leave content hidden if the observer doesn't fire.
    setTimeout(function () {
      document.querySelectorAll("[data-reveal-group] > *:not(.is-in)").forEach(function (c) { c.classList.add("is-in"); });
    }, 2500);
  }

  /* Account menu: click to toggle, close on outside click or Escape. */
  var btn = document.getElementById("account-btn");
  var menu = document.getElementById("account-menu");
  if (btn && menu) {
    var close = function (focusBtn) {
      if (menu.hidden) return;
      menu.hidden = true; btn.setAttribute("aria-expanded", "false");
      if (focusBtn) btn.focus();
    };
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = menu.hidden;
      menu.hidden = !open; btn.setAttribute("aria-expanded", String(open));
      if (open) { var first = menu.querySelector("a, button"); if (first) first.focus({ preventScroll: true }); }
    });
    document.addEventListener("click", function (e) { if (!menu.contains(e.target) && e.target !== btn) close(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(true); });
  }
})();
