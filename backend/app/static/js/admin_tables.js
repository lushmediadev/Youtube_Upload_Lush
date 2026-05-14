(function () {
function initAdminTables() {
  function restoreWindowScroll(y) {
    const nextY = Number(y);
    if (!Number.isFinite(nextY) || nextY <= 0) {
      return;
    }
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        window.scrollTo(0, nextY);
      });
    });
  }

  function clampPage(value, totalPages) {
    const safeTotal = Math.max(totalPages, 1);
    return Math.min(Math.max(value, 1), safeTotal);
  }

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }

  function buildPageNumbers(totalPages, currentPage) {
    if (totalPages <= 5) {
      return Array.from({ length: totalPages }, function (_, index) {
        return index + 1;
      });
    }

    const pages = [1];
    const start = Math.max(2, currentPage - 1);
    const end = Math.min(totalPages - 1, currentPage + 1);

    if (start > 2) {
      pages.push("ellipsis-left");
    }

    for (let page = start; page <= end; page += 1) {
      pages.push(page);
    }

    if (end < totalPages - 1) {
      pages.push("ellipsis-right");
    }

    pages.push(totalPages);
    return pages;
  }

  function parseDateValue(value) {
    const trimmed = String(value || "").trim();
    const match = trimmed.match(
      /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/
    );
    if (!match) {
      return null;
    }
    const [, day, month, year, hour = "0", minute = "0", second = "0"] = match;
    return Date.UTC(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second)
    );
  }

  function parseNumberValue(value) {
    const trimmed = String(value || "")
      .replace(/\s+/g, "")
      .replace(/,/g, ".");
    if (!trimmed || /[a-zA-Z]/.test(trimmed)) {
      return null;
    }
    const numeric = trimmed.replace(/[^\d.+-]/g, "");
    if (!numeric || !/^[-+]?\d*\.?\d+$/.test(numeric)) {
      return null;
    }
    const parsed = Number(numeric);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function compareValues(valueA, valueB) {
    if (valueA.type === valueB.type) {
      if (valueA.value === valueB.value) {
        return 0;
      }
      return valueA.value > valueB.value ? 1 : -1;
    }

    const order = { number: 0, date: 1, text: 2 };
    return order[valueA.type] > order[valueB.type] ? 1 : -1;
  }

  function buildSortValue(rawValue) {
    const text = String(rawValue || "").replace(/\s+/g, " ").trim();
    const dateValue = parseDateValue(text);
    if (dateValue !== null) {
      return { type: "date", value: dateValue };
    }

    const numberValue = parseNumberValue(text);
    if (numberValue !== null) {
      return { type: "number", value: numberValue };
    }

    return { type: "text", value: normalizeText(text) };
  }

  function invokeDeleteControl(control) {
    if (!control) {
      return Promise.resolve();
    }

    if (control.tagName === "A") {
      return fetch(control.href, {
        method: "GET",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("Delete request failed.");
        }
        return response;
      });
    }

    if (control.matches("[data-bulk-delete-button]")) {
      const method = String(control.dataset.bulkDeleteMethod || "DELETE").toUpperCase();
      const url = control.dataset.bulkDeleteUrl || "";
      const body = String(control.dataset.bulkDeleteBody || "").trim();
      if (!url) {
        return Promise.reject(new Error("Delete request is missing URL."));
      }
      const options = {
        method: method,
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      };
      if (body && method !== "GET") {
        options.headers["Content-Type"] = "application/json";
        options.body = body;
      }
      return fetch(url, options).then(function (response) {
        if (!response.ok) {
          throw new Error("Delete request failed.");
        }
        return response;
      });
    }

    const form = control;
    const method = String(form.getAttribute("method") || "GET").toUpperCase();
    const action = form.getAttribute("action") || window.location.href;
    const formData = new FormData(form);

    if (method === "GET") {
      const url = new URL(action, window.location.origin);
      formData.forEach(function (value, key) {
        url.searchParams.append(key, String(value));
      });
      return fetch(url.toString(), {
        method: "GET",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("Delete request failed.");
        }
        return response;
      });
    }

    const body = new URLSearchParams();
    formData.forEach(function (value, key) {
      body.append(key, String(value));
    });

    return fetch(action, {
      method: method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: body.toString(),
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("Delete request failed.");
      }
      return response;
    });
  }

  function buildHeaderMarkup(label, isActive, direction, alignEnd) {
    return (
      '<button type="button" class="sortable-button' +
      (isActive ? " is-active" : "") +
      (alignEnd ? " is-end" : "") +
      '" data-admin-sort-trigger>' +
      '  <span class="sortable-button-label">' +
      label +
      "</span>" +
      '  <span class="sort-arrows" aria-hidden="true">' +
      '    <span class="sort-up' +
      (isActive && direction === "asc" ? " is-on" : "") +
      '">↑</span>' +
      '    <span class="sort-down' +
      (isActive && direction === "desc" ? " is-on" : "") +
      '">↓</span>' +
      "  </span>" +
      "</button>"
    );
  }

  function requestDeletePageConfirmation(count, requiredPass) {
    const pass = String(requiredPass || "");
    if (!pass) {
      return Promise.resolve(window.confirm("X\u00f3a " + count + " d\u00f2ng \u0111ang hi\u1ec3n th\u1ecb tr\u00ean trang n\u00e0y?"));
    }
    return new Promise(function (resolve) {
      if (typeof HTMLDialogElement === "undefined") {
        if (!window.confirm("X\u00f3a " + count + " d\u00f2ng \u0111ang hi\u1ec3n th\u1ecb tr\u00ean trang n\u00e0y?")) {
          resolve(false);
          return;
        }
        resolve(window.prompt("Nh\u1eadp pass xo\u00e1 trang") === pass);
        return;
      }

      const dialog = document.createElement("dialog");
      dialog.className = "admin-dialog";
      dialog.innerHTML =
        '<div class="panel">' +
        '  <form method="dialog" class="space-y-4 p-5" data-delete-page-confirm-form>' +
        '    <div>' +
        '      <h3 class="section-title">X\u00f3a trang hi\u1ec7n t\u1ea1i</h3>' +
        '      <p class="section-note mt-1">Thao t\u00e1c n\u00e0y s\u1ebd x\u00f3a ' +
        count +
        ' d\u00f2ng \u0111ang hi\u1ec3n th\u1ecb tr\u00ean trang hi\u1ec7n t\u1ea1i.</p>' +
        "    </div>" +
        '    <div>' +
        '      <label class="field-label" for="admin-delete-page-pass">Nh\u1eadp pass x\u00f3a</label>' +
        '      <input class="toolbar-input" id="admin-delete-page-pass" type="password" autocomplete="off" inputmode="numeric" placeholder="Nh\u1eadp pass">' +
        '      <p class="mt-2 text-[12px] font-semibold text-rose-600" data-delete-page-error hidden>Pass kh\u00f4ng \u0111\u00fang.</p>' +
        "    </div>" +
        '    <div class="flex justify-end gap-2 border-t border-slate-200 pt-4">' +
        '      <button type="button" class="action-btn action-btn-slate" data-delete-page-cancel>\u0110\u00f3ng</button>' +
        '      <button type="submit" class="action-btn action-btn-rose">X\u00e1c nh\u1eadn x\u00f3a</button>' +
        "    </div>" +
        "  </form>" +
        "</div>";

      document.body.appendChild(dialog);
      const form = dialog.querySelector("[data-delete-page-confirm-form]");
      const input = dialog.querySelector("#admin-delete-page-pass");
      const error = dialog.querySelector("[data-delete-page-error]");
      const cancelButton = dialog.querySelector("[data-delete-page-cancel]");
      let settled = false;

      function finish(value) {
        if (settled) {
          return;
        }
        settled = true;
        dialog.close();
        dialog.remove();
        resolve(value);
      }

      cancelButton.addEventListener("click", function () {
        finish(false);
      });
      dialog.addEventListener("close", function () {
        finish(false);
      });
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (String(input.value || "").trim() !== pass) {
          if (error) {
            error.hidden = false;
          }
          input.focus();
          input.select();
          return;
        }
        finish(true);
      });

      dialog.showModal();
      input.focus();
    });
  }

  document.querySelectorAll("table[data-admin-list-table]").forEach(function (table) {
    if (table.dataset.adminTableReady === "true") {
      return;
    }

    const tbody = table.tBodies[0];
    const tableShell = table.closest(".table-shell");
    if (!tbody || !tableShell) {
      return;
    }

    const panel = tableShell.parentElement;
    let rows = Array.from(tbody.querySelectorAll(":scope > tr"));
    const headers = table.tHead ? Array.from(table.tHead.rows[0].cells) : [];
    const pageSize = Math.max(parseInt(table.dataset.pageSize || "10", 10) || 10, 1);
    const totalColumns = headers.length || 1;
    const tableStateStorageKey =
      "ytlush:admin-table-state:" +
      window.location.pathname +
      ":" +
      (table.dataset.adminListTable || table.id || "default");
    let currentPage = 1;
    let sortIndex = null;
    let sortDirection = "asc";

    function findDirectChild(predicate) {
      return Array.from(panel.children).find(function (child) {
        return child.nodeType === 1 && predicate(child);
      });
    }

    const headerBlock = findDirectChild(function (child) {
      return child !== tableShell && child.querySelector(".section-title");
    });

    if (headerBlock && headerBlock.dataset.adminTableHeaderReady !== "true") {
      const copy = document.createElement("div");
      copy.className = "admin-table-header-copy";
      while (headerBlock.firstChild) {
        copy.appendChild(headerBlock.firstChild);
      }
      headerBlock.classList.add("admin-table-header");
      headerBlock.appendChild(copy);
      headerBlock.dataset.adminTableHeaderReady = "true";
    }

    let toolbar = panel.querySelector("[data-admin-table-toolbar]");
    if (!toolbar) {
      toolbar = document.createElement("div");
      toolbar.setAttribute("data-admin-table-toolbar", "");
      toolbar.className = "admin-table-toolbar";
      toolbar.innerHTML =
        '<label class="admin-table-search-shell">' +
        '  <i data-lucide="search" class="h-4 w-4 text-slate-400"></i>' +
        '  <input type="search" class="admin-table-search-input" data-admin-table-search placeholder="Tìm kiếm nhanh..." autocomplete="off">' +
        "</label>";

      if (headerBlock) {
        toolbar.classList.add("is-inline");
        headerBlock.appendChild(toolbar);
      } else {
        toolbar.classList.add("is-block");
        panel.insertBefore(toolbar, tableShell);
      }
    }

    const toolbarExtraSource = findDirectChild(function (child) {
      return child.hasAttribute("data-admin-table-toolbar-extra");
    });
    if (toolbarExtraSource) {
      while (toolbarExtraSource.firstChild) {
        toolbar.appendChild(toolbarExtraSource.firstChild);
      }
      toolbarExtraSource.remove();
    }

    let footer = findDirectChild(function (child) {
      return child.hasAttribute("data-admin-table-footer");
    });
    if (!footer) {
      footer = document.createElement("div");
      footer.setAttribute("data-admin-table-footer", "");
      footer.className = "admin-table-footer";
      footer.innerHTML =
        '<p class="admin-table-summary" data-admin-table-summary></p>' +
        '<div class="admin-table-footer-actions">' +
        '  <div class="admin-table-pagination" data-admin-table-pagination></div>' +
        '  <button type="button" class="action-btn action-btn-slate admin-table-delete-page" data-admin-table-delete-page>' +
        '    <i data-lucide="trash-2" class="h-3.5 w-3.5"></i>' +
        "    Xóa trang" +
        "  </button>" +
        '  <div class="admin-table-extra-actions" data-admin-table-extra-actions></div>' +
        "</div>";
      panel.appendChild(footer);
    }

    const footerExtraSource = findDirectChild(function (child) {
      return child.hasAttribute("data-admin-table-footer-extra");
    });
    const footerExtraTarget = footer.querySelector("[data-admin-table-extra-actions]");
    if (footerExtraSource && footerExtraTarget) {
      while (footerExtraSource.firstChild) {
        footerExtraTarget.appendChild(footerExtraSource.firstChild);
      }
      footerExtraSource.remove();
    }

    const searchInput = toolbar.querySelector("[data-admin-table-search]");
    const summaryNode = footer.querySelector("[data-admin-table-summary]");
    const paginationNode = footer.querySelector("[data-admin-table-pagination]");
    const deletePageButton = footer.querySelector("[data-admin-table-delete-page]");
    let emptyRow = null;

    function persistTableUiState() {
      try {
        window.sessionStorage.setItem(
          tableStateStorageKey,
          JSON.stringify({
            path: window.location.pathname,
            search: searchInput ? searchInput.value : "",
            currentPage: currentPage,
            sortIndex: sortIndex,
            sortDirection: sortDirection,
            scrollY: window.scrollY || window.pageYOffset || 0,
          })
        );
      } catch (_error) {
        // Ignore transient storage issues.
      }
    }

    function restoreTableUiState() {
      try {
        const raw = window.sessionStorage.getItem(tableStateStorageKey);
        if (!raw) {
          return;
        }
        window.sessionStorage.removeItem(tableStateStorageKey);
        const payload = JSON.parse(raw);
        if (!payload || payload.path !== window.location.pathname) {
          return;
        }
        if (searchInput && typeof payload.search === "string") {
          searchInput.value = payload.search;
        }
        if (Number.isInteger(payload.sortIndex) && payload.sortIndex >= 0) {
          sortIndex = payload.sortIndex;
        }
        if (payload.sortDirection === "desc" || payload.sortDirection === "asc") {
          sortDirection = payload.sortDirection;
        }
        if (Number.isInteger(payload.currentPage) && payload.currentPage > 0) {
          currentPage = payload.currentPage;
        }
        restoreWindowScroll(payload.scrollY);
      } catch (_error) {
        // Ignore malformed restore payloads.
      }
    }

    headers.forEach(function (headerCell, index) {
      if (!headerCell.dataset.adminSortLabel) {
        headerCell.dataset.adminSortLabel = headerCell.textContent.replace(/\s+/g, " ").trim();
      }
    });

    function refreshHeaderButtons() {
      headers.forEach(function (headerCell, index) {
        const label = headerCell.dataset.adminSortLabel || "";
        const isActive = sortIndex === index;
        const alignEnd = headerCell.classList.contains("text-right");
        headerCell.innerHTML = buildHeaderMarkup(label, isActive, sortDirection, alignEnd);
      });

      table.querySelectorAll("[data-admin-sort-trigger]").forEach(function (button, index) {
        button.addEventListener("click", function () {
          if (sortIndex === index) {
            sortDirection = sortDirection === "asc" ? "desc" : "asc";
          } else {
            sortIndex = index;
            sortDirection = "asc";
          }
          currentPage = 1;
          refreshHeaderButtons();
          applyTableState();
        });
      });
    }

    function ensureEmptyRow(shouldShow) {
      if (emptyRow && !tbody.contains(emptyRow)) {
        emptyRow = null;
      }

      if (!shouldShow) {
        if (emptyRow) {
          emptyRow.remove();
          emptyRow = null;
        }
        return;
      }

      if (!emptyRow) {
        emptyRow = document.createElement("tr");
        emptyRow.setAttribute("data-admin-table-empty", "true");
        emptyRow.innerHTML =
          '<td colspan="' +
          totalColumns +
          '" class="admin-table-empty">Không có kết quả phù hợp.</td>';
        tbody.appendChild(emptyRow);
      }
    }

    function rowDeleteControl(row) {
      return row.querySelector("[data-bulk-delete-form], a[data-bulk-delete-link], [data-bulk-delete-button]");
    }

    function refreshBodyRows() {
      rows = Array.from(tbody.querySelectorAll(":scope > tr")).filter(function (row) {
        return !row.hasAttribute("data-admin-table-empty");
      });
      bindRowDeleteControls();
    }

    function bindRowDeleteControls() {
      rows.forEach(function (row) {
        const control = rowDeleteControl(row);
        if (!control || control.dataset.scrollRestoreBound === "true") {
          return;
        }
        control.dataset.scrollRestoreBound = "true";
        if (control.tagName === "A") {
          control.addEventListener("click", function () {
            persistTableUiState();
          });
          return;
        }
        control.addEventListener("submit", function () {
          persistTableUiState();
        });
      });
    }

    function getRowCellText(row, columnIndex) {
      const cell = row.cells[columnIndex];
      if (!cell) {
        return "";
      }
      const explicitNode = cell.querySelector("[data-sort-value]");
      if (explicitNode) {
        return explicitNode.getAttribute("data-sort-value") || "";
      }
      const explicitCellValue = cell.getAttribute("data-sort-value");
      if (explicitCellValue) {
        return explicitCellValue;
      }
      return cell.textContent.replace(/\s+/g, " ").trim();
    }

    function filteredRows() {
      const keyword = normalizeText(searchInput ? searchInput.value : "");
      let result = rows.filter(function (row) {
        if (row.hasAttribute("data-admin-table-empty")) {
          return false;
        }
        return !keyword || normalizeText(row.dataset.searchText || row.textContent).indexOf(keyword) !== -1;
      });

      if (sortIndex !== null) {
        result = result.slice().sort(function (rowA, rowB) {
          const valueA = buildSortValue(getRowCellText(rowA, sortIndex));
          const valueB = buildSortValue(getRowCellText(rowB, sortIndex));
          const comparison = compareValues(valueA, valueB);
          return sortDirection === "asc" ? comparison : comparison * -1;
        });
      }

      return result;
    }

    function currentPageRows(items) {
      const totalPages = Math.max(Math.ceil(items.length / pageSize), 1);
      currentPage = clampPage(currentPage, totalPages);
      const startIndex = (currentPage - 1) * pageSize;
      return items.slice(startIndex, startIndex + pageSize);
    }

    function buildPagination(totalPages) {
      paginationNode.innerHTML = "";

      function createPageButton(label, page, disabled, active) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "admin-table-page-btn";
        if (active) {
          button.classList.add("is-active");
        }
        if (label === "prev") {
          button.innerHTML = "&lsaquo;";
          button.setAttribute("aria-label", "Trang trước");
        } else if (label === "next") {
          button.innerHTML = "&rsaquo;";
          button.setAttribute("aria-label", "Trang sau");
        } else if (label === "ellipsis-left" || label === "ellipsis-right") {
          button.textContent = "...";
          button.disabled = true;
          button.classList.add("is-ghost");
          return button;
        } else {
          button.textContent = String(label);
        }
        button.disabled = !!disabled;
        if (!disabled && page) {
          button.addEventListener("click", function () {
            currentPage = page;
            applyTableState();
          });
        }
        return button;
      }

      paginationNode.appendChild(createPageButton("prev", currentPage - 1, currentPage <= 1, false));
      buildPageNumbers(totalPages, currentPage).forEach(function (item) {
        if (typeof item === "number") {
          paginationNode.appendChild(createPageButton(item, item, false, item === currentPage));
        } else {
          paginationNode.appendChild(createPageButton(item, null, true, false));
        }
      });
      paginationNode.appendChild(createPageButton("next", currentPage + 1, currentPage >= totalPages, false));
    }

    async function handleDeletePage() {
      const items = filteredRows();
      const pageRows = currentPageRows(items);
      const deletableControls = pageRows
        .map(function (row) {
          return rowDeleteControl(row);
        })
        .filter(Boolean);

      if (!deletableControls.length) {
        return;
      }

      const confirmed = await requestDeletePageConfirmation(deletableControls.length, table.dataset.adminDeletePagePass);
      if (!confirmed) {
        return;
      }

      const originalHtml = deletePageButton.innerHTML;
      deletePageButton.disabled = true;
      deletePageButton.innerHTML = "Đang xóa...";

      try {
        for (const control of deletableControls) {
          await invokeDeleteControl(control);
        }
        persistTableUiState();
        window.location.reload();
      } catch (error) {
        console.error(error);
        if (typeof window.showAdminToast === "function") {
          window.showAdminToast("Không thể xóa trang hiện tại. Hãy thử lại.", "error");
        }
        deletePageButton.innerHTML = originalHtml;
        applyTableState();
      }
    }

    function applyTableState() {
      const items = filteredRows();
      const total = items.length;
      const totalPages = Math.max(Math.ceil(total / pageSize), 1);
      const pageRows = currentPageRows(items);
      const startIndex = total ? (currentPage - 1) * pageSize : 0;
      const from = total ? startIndex + 1 : 0;
      const to = total ? startIndex + pageRows.length : 0;

      const orderedRows = pageRows.concat(
        rows.filter(function (row) {
          return pageRows.indexOf(row) === -1;
        })
      );

      orderedRows.forEach(function (row) {
        row.style.display = pageRows.indexOf(row) !== -1 ? "" : "none";
        tbody.appendChild(row);
      });

      ensureEmptyRow(total === 0);

      if (summaryNode) {
        summaryNode.textContent = "Hiển thị " + from + " đến " + to + " trong " + total + " kết quả";
      }

      if (deletePageButton) {
        deletePageButton.disabled = !pageRows.some(function (row) {
          return !!rowDeleteControl(row);
        });
      }

      buildPagination(totalPages);
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        currentPage = 1;
        applyTableState();
      });
    }

    if (deletePageButton) {
      deletePageButton.addEventListener("click", handleDeletePage);
    }

    restoreTableUiState();
    refreshHeaderButtons();
    refreshBodyRows();
    applyTableState();

    if (typeof window.hydrateAdminIcons === "function") {
      window.hydrateAdminIcons();
    } else if (window.lucide) {
      window.lucide.createIcons({ attrs: { "stroke-width": 1.75 } });
    }

    table.addEventListener("admin-table:refresh-rows", function () {
      refreshBodyRows();
      applyTableState();
      if (typeof window.hydrateAdminIcons === "function") {
        window.hydrateAdminIcons();
      } else if (window.lucide) {
        window.lucide.createIcons({ attrs: { "stroke-width": 1.75 } });
      }
    });

    table.dataset.adminTableReady = "true";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAdminTables, { once: true });
} else {
  initAdminTables();
}
})();
