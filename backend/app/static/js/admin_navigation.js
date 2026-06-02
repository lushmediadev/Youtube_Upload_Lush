(function () {
  const PREFETCH_TTL_MS = 20000;
  const MAX_CACHE_ENTRIES = 8;
  const FETCH_TIMEOUT_MS = 2200;
  const cache = new Map();
  const inFlight = new Map();

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
    } catch (error) {
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

  function trimCache() {
    while (cache.size > MAX_CACHE_ENTRIES) {
      const firstKey = cache.keys().next().value;
      cache.delete(firstKey);
    }
  }

  function cachedHtml(url) {
    const key = cacheKey(url);
    const entry = cache.get(key);
    if (!entry) {
      return "";
    }
    if (Date.now() - entry.createdAt > PREFETCH_TTL_MS) {
      cache.delete(key);
      return "";
    }
    cache.delete(key);
    cache.set(key, entry);
    return entry.html;
  }

  function fetchWithTimeout(url) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(function () {
      controller.abort();
    }, FETCH_TIMEOUT_MS);

    return fetch(url.toString(), {
      method: "GET",
      credentials: "same-origin",
      headers: {
        Accept: "text/html,application/xhtml+xml",
        "X-Requested-With": "XMLHttpRequest",
        "X-Admin-Prefetch": "1",
      },
      cache: "force-cache",
      signal: controller.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Internal page prefetch failed.");
        }
        return response.text();
      })
      .finally(function () {
        window.clearTimeout(timeoutId);
      });
  }

  function prefetchUrl(url) {
    const key = cacheKey(url);
    const cached = cachedHtml(url);
    if (cached) {
      return Promise.resolve(cached);
    }
    if (inFlight.has(key)) {
      return inFlight.get(key);
    }
    const request = fetchWithTimeout(url)
      .then(function (html) {
        if (!/<main[\s>]/i.test(html) || !html.includes("app-shell-content")) {
          throw new Error("Prefetched response is not an admin page.");
        }
        cache.set(key, { html: html, createdAt: Date.now() });
        trimCache();
        return html;
      })
      .finally(function () {
        inFlight.delete(key);
      });
    inFlight.set(key, request);
    return request;
  }

  function replaceDocument(url, html) {
    window.history.pushState({}, "", url.pathname + url.search + url.hash);
    document.open();
    document.write(html);
    document.close();
  }

  function warmLink(link) {
    if (!shouldHandleLink(link, null)) {
      return;
    }
    const url = normalizeInternalUrl(link.href);
    if (url) {
      prefetchUrl(url).catch(function () {});
    }
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

    event.preventDefault();
    document.documentElement.classList.add("admin-fast-nav-loading");

    const html = cachedHtml(url);
    if (html) {
      replaceDocument(url, html);
      return;
    }

    prefetchUrl(url)
      .then(function (nextHtml) {
        replaceDocument(url, nextHtml);
      })
      .catch(function () {
        window.location.assign(url.toString());
      });
  }

  function warmVisibleNavigation() {
    const links = Array.from(document.querySelectorAll("a.nav-item[href], .module-tab[href]"));
    let index = 0;
    function runNext() {
      if (index >= links.length) {
        return;
      }
      warmLink(links[index]);
      index += 1;
      window.setTimeout(runNext, 220);
    }
    runNext();
  }

  document.addEventListener("click", handleClick);
  document.addEventListener(
    "pointerenter",
    function (event) {
      const link = event.target.closest && event.target.closest("a[href]");
      if (link) {
        warmLink(link);
      }
    },
    true
  );
  document.addEventListener(
    "focusin",
    function (event) {
      const link = event.target.closest && event.target.closest("a[href]");
      if (link) {
        warmLink(link);
      }
    },
    true
  );

  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(warmVisibleNavigation, { timeout: 1800 });
  } else {
    window.setTimeout(warmVisibleNavigation, 700);
  }
})();
