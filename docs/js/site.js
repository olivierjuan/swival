// Swival — static site JavaScript
(function () {
  "use strict";

  // ===== Utilities =====
  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function prefersReducedMotion() {
    return (
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  // ===== Tabs =====
  function initTabs() {
    var tabButtons = document.querySelectorAll(".tab-button");
    var tabPanels = document.querySelectorAll(".tab-panel");
    if (!tabButtons.length) return;

    tabButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var target = btn.getAttribute("data-tab");

        tabButtons.forEach(function (b) { b.classList.remove("active"); });
        tabPanels.forEach(function (p) { p.classList.remove("active"); });

        btn.classList.add("active");
        var panel = document.getElementById("tab-" + target);
        if (panel) panel.classList.add("active");
      });
    });
  }

  // ===== Scroll-triggered reveal animations =====
  function initReveal() {
    var animated = document.querySelectorAll("[data-animate]");
    if (!animated.length) return;

    if (!("IntersectionObserver" in window)) {
      animated.forEach(function (el) { el.classList.add("visible"); });
      return;
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1, rootMargin: "0px 0px -40px 0px" }
    );

    animated.forEach(function (el) { observer.observe(el); });
  }

  // ===== Back to top =====
  function initBackToTop() {
    var backToTop = document.querySelector(".back-to-top");
    if (!backToTop) return;

    window.addEventListener("scroll", function () {
      if (window.scrollY > 400) {
        backToTop.classList.add("visible");
      } else {
        backToTop.classList.remove("visible");
      }
    }, { passive: true });

    backToTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
    });
  }

  // ===== Code blocks: wrap, add language label and copy button =====
  function initCodeBlocks() {
    var pres = document.querySelectorAll(".docs-content pre");
    if (!pres.length) return;

    pres.forEach(function (pre) {
      if (pre.parentNode && pre.parentNode.classList.contains("code-block")) {
        return; // already wrapped
      }
      var wrapper = document.createElement("div");
      wrapper.className = "code-block";
      pre.parentNode.insertBefore(wrapper, pre);
      wrapper.appendChild(pre);

      // Detect language from the inner <code class="language-...">
      var inner = pre.querySelector("code");
      var lang = null;
      if (inner) {
        var classes = (inner.className || "").split(/\s+/);
        for (var i = 0; i < classes.length; i++) {
          var m = classes[i].match(/^language-(.+)$/);
          if (m) { lang = m[1]; break; }
        }
      }
      if (lang) {
        wrapper.classList.add("has-lang");
        var label = document.createElement("span");
        label.className = "code-lang";
        label.textContent = lang;
        wrapper.appendChild(label);
      }

      // Copy button
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-button";
      btn.setAttribute("aria-label", "Copy code to clipboard");
      btn.innerHTML =
        '<svg class="copy-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>' +
          '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>' +
        '</svg>' +
        '<svg class="check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<polyline points="20 6 9 17 4 12"></polyline>' +
        '</svg>' +
        '<span class="copy-label">Copy</span>';

      btn.addEventListener("click", function () {
        var text = pre.innerText;
        var done = function () {
          btn.classList.add("copied");
          var lbl = btn.querySelector(".copy-label");
          if (lbl) lbl.textContent = "Copied";
          window.setTimeout(function () {
            btn.classList.remove("copied");
            if (lbl) lbl.textContent = "Copy";
          }, 1600);
        };
        var fail = function () {
          var lbl = btn.querySelector(".copy-label");
          if (lbl) lbl.textContent = "Copy failed";
          window.setTimeout(function () {
            if (lbl) lbl.textContent = "Copy";
          }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, fail);
        } else {
          // Fallback for older browsers
          try {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            var ok = document.execCommand("copy");
            document.body.removeChild(ta);
            if (ok) done(); else fail();
          } catch (e) { fail(); }
        }
      });

      wrapper.appendChild(btn);
    });
  }

  // ===== Heading anchor links on h2/h3 inside .docs-content =====
  function initHeadingAnchors() {
    var headings = document.querySelectorAll(
      ".docs-content h2[id], .docs-content h3[id]"
    );
    if (!headings.length) return;

    headings.forEach(function (h) {
      if (h.querySelector(".heading-anchor")) return;
      var id = h.getAttribute("id");
      if (!id) return;
      var a = document.createElement("a");
      a.className = "heading-anchor";
      a.href = "#" + id;
      a.setAttribute("aria-label", "Link to " + (h.textContent || "").trim());
      a.textContent = "#";
      h.appendChild(a);
    });
  }

  // ===== Build the right-rail page TOC =====
  function buildPageTOC() {
    var container = document.querySelector(".page-toc-list");
    var content = document.querySelector(".docs-content");
    if (!container || !content) return;

    var headings = content.querySelectorAll("h2[id], h3[id]");
    if (headings.length < 2) {
      // Hide the rail if there is nothing meaningful to show
      var rail = document.querySelector(".page-toc");
      if (rail) rail.style.display = "none";
      return;
    }

    var ul = document.createElement("ul");
    headings.forEach(function (h) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "#" + h.id;
      a.textContent = h.textContent.replace(/#$/, "").trim();
      if (h.tagName.toLowerCase() === "h3") {
        a.classList.add("toc-h3");
      }
      a.setAttribute("data-toc-target", h.id);
      li.appendChild(a);
      ul.appendChild(li);
    });
    container.appendChild(ul);

    initTOCHighlight(ul, headings);
  }

  function initTOCHighlight(tocList, headings) {
    if (!("IntersectionObserver" in window)) return;

    var links = {};
    tocList.querySelectorAll("a[data-toc-target]").forEach(function (a) {
      links[a.getAttribute("data-toc-target")] = a;
    });

    var visible = new Map(); // id -> intersectionRatio
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            visible.set(entry.target.id, entry.intersectionRatio);
          } else {
            visible.delete(entry.target.id);
          }
        });
        // Pick the topmost visible heading; fall back to the last one
        // whose top is above the viewport's effective top.
        var activeId = null;
        var bestTop = -Infinity;
        headings.forEach(function (h) {
          if (visible.has(h.id)) {
            var top = h.getBoundingClientRect().top;
            if (top <= 120 && top > bestTop) {
              bestTop = top;
              activeId = h.id;
            }
          }
        });
        if (!activeId) {
          // No heading currently visible: pick the last one whose top is above 120
          headings.forEach(function (h) {
            var top = h.getBoundingClientRect().top;
            if (top <= 120) activeId = h.id;
          });
        }
        Object.keys(links).forEach(function (id) {
          links[id].classList.toggle("active", id === activeId);
        });
      },
      {
        // Fire when a heading enters/leaves the band just below the sticky header
        rootMargin: "-80px 0px -65% 0px",
        threshold: [0, 0.25, 0.5, 0.75, 1]
      }
    );
    headings.forEach(function (h) { observer.observe(h); });
  }

  // ===== Bootstrap =====
  ready(function () {
    initTabs();
    initReveal();
    initBackToTop();
    initCodeBlocks();
    initHeadingAnchors();
    buildPageTOC();
  });
})();
