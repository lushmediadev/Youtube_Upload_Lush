(function () {
  const prefetched = new Set();

  function isPlainLeftClick(event) {
    return (
      event.button === 0 &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.shiftKey &&
      !event.altKey
    );
  }

  function normalizeInternalUrl(rawHref) {
    let url;
    try {
      url = new URL(rawHref, window.location.href);
    } catch (_error) {
      return null;
    }
    if (url.origin !== window.location.origin) {
      return null;
    }
    const allowedWorkspaceRoute = url.pathname === "/app" || url.pathname === "/app/live";
    const allowedAdminRoute = url.pathname.startsWith("/admin/");
    if (!allowedAdminRoute && !allowedWorkspaceRoute) {
      return null;
    }
    if (url.pathname === "/admin/logout" || url.pathname.endsWith("/logout")) {
      return null;
    }
    return url;
  }

  function shouldHandleLink(link, event) {
    if (!link || !link.href) {
      return false;
    }
    if (event && !isPlainLeftClick(event)) {
      return false;
    }
    if (link.target && link.target !== "_self") {
      return false;
    }
    if (link.hasAttribute("download") || link.dataset.adminHardNav === "true") {
      return false;
    }
    const url = normalizeInternalUrl(link.href);
    if (!url) {
      return false;
    }
    const current = new URL(window.location.href);
    return url.pathname + url.search !== current.pathname + current.search;
  }

  function cacheKey(url) {
    return url.pathname + url.search;
  }

  function prefetchUrl(url, highPriority) {
    const key = cacheKey(url);
    if (prefetched.has(key)) {
      return;
    }
    prefetched.add(key);

    const link = document.createElement("link");
    link.rel = "prefetch";
    link.as = "document";
    link.href = url.toString();
    if ("fetchPriority" in link) {
      link.fetchPriority = highPriority ? "high" : "low";
    }
    link.dataset.appNavigationPrefetch = key;
    document.head.appendChild(link);
  }

  function warmLink(link, highPriority) {
    if (!shouldHandleLink(link, null)) {
      return;
    }
    const url = normalizeInternalUrl(link.href);
    if (url) {
      prefetchUrl(url, highPriority);
    }
  }

  function setPendingNavigationState(link) {
    if (link.matches(".module-tab")) {
      const tabList = link.parentElement;
      if (tabList) {
        Array.from(tabList.querySelectorAll(":scope > a.module-tab[href]")).forEach(function (item) {
          const active = item === link;
          item.classList.toggle("active", active);
          if (active) {
            item.setAttribute("aria-current", "page");
          } else {
            item.removeAttribute("aria-current");
          }
        });
      }
      return;
    }

    const nav = link.closest("nav, .mobile-shell-nav");
    const items = nav ? Array.from(nav.querySelectorAll("a.nav-item[href]")) : [];
    items.forEach(function (item) {
      const active = item === link;
      item.classList.toggle("active", active);
      if (active) {
        item.setAttribute("aria-current", "page");
      } else {
        item.removeAttribute("aria-current");
      }
    });
  }

  function handleClick(event) {
    const link = event.target.closest("a[href]");
    if (!shouldHandleLink(link, event)) {
      return;
    }
    const url = normalizeInternalUrl(link.href);
    if (!url) {
      return;
    }

    setPendingNavigationState(link);
    prefetchUrl(url, true);
  }

  function warmVisibleNavigation() {
    const links = Array.from(document.querySelectorAll("a.nav-item[href], .module-tab[href]"));
    let index = 0;

    function runNext() {
      if (index >= links.length) {
        return;
      }
      warmLink(links[index], false);
      index += 1;
      window.setTimeout(runNext, 160);
    }

    runNext();
  }

  document.addEventListener("click", handleClick);
  document.addEventListener(
    "pointerdown",
    function (event) {
      if (!isPlainLeftClick(event)) {
        return;
      }
      const link = event.target.closest && event.target.closest("a[href]");
      if (link) {
        warmLink(link, true);
      }
    },
    true
  );
  document.addEventListener(
    "pointerenter",
    function (event) {
      const link = event.target.closest && event.target.closest("a[href]");
      if (link) {
        warmLink(link, true);
      }
    },
    true
  );
  document.addEventListener(
    "focusin",
    function (event) {
      const link = event.target.closest && event.target.closest("a[href]");
      if (link) {
        warmLink(link, true);
      }
    },
    true
  );

  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(warmVisibleNavigation, { timeout: 900 });
  } else {
    window.setTimeout(warmVisibleNavigation, 350);
  }
})();
