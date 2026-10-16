const state = {
  user: null, tickets: [], technicians: [], users: [], customers: [],
  selectedCustomerId: null, view: "board", month: new Date(),
  boardStatus: "received", calendarDay: null,
};
const mobileLayout = window.matchMedia("(max-width: 1024px)");
let pushRegistration = null;
let pushSettings = null;
let addressTimer = null;
let addressRequest = null;
let addressMatches = [];
let addressIndex = -1;
const t = I18n.t;
const statuses = [
  { key: "received", label: "Received" },
  { key: "in_progress", label: "In progress" },
  { key: "problem", label: "Problem" },
];
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const hasRole = (...roles) => roles.some((role) => state.user?.roles.includes(role));
const canManage = () => hasRole("admin", "dispatcher");
const roleName = (roles) => roles.map((role) => ({
  admin: "Admin", dispatcher: "Call receiver", technician: "Technician",
}[role])).map((name) => t(name)).join(" · ");

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
  if (response.status === 204) return null;
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && url !== "/api/login") showLogin();
    throw new Error(t(result.error || "Something went wrong."));
  }
  return result;
}

function showLogin() {
  state.user = null;
  $$(".modal:not(.hidden)").reverse().forEach((modal) => closeModal(modal.id));
  setMenuOpen(false);
  state.selectedCustomerId = null;
  $(".customers-layout").classList.remove("show-customer");
  $("#customer-back").classList.add("hidden");
  $("#app").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
}

function showApp(user) {
  state.user = user;
  I18n.set(user.language);
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-name").textContent = user.username;
  $("#user-role").textContent = roleName(user.roles);
  $("#user-avatar").textContent = user.username.slice(0, 1).toUpperCase();
  $("#users-nav").classList.toggle("hidden", !hasRole("admin"));
  $("#customers-nav").classList.toggle("hidden", !canManage());
  $("#mobile-customers-nav").classList.toggle("hidden", !canManage());
  $$(".calendar-export").forEach((link) => link.classList.toggle("hidden", !user));
  $("#new-ticket").classList.toggle("hidden", !canManage());
  $("#assignee-field").classList.toggle("hidden", !canManage());
  $("#customer-lookup-field").classList.toggle("hidden", !canManage());
  showView("board");
}

async function changeLanguage(event) {
  const language = event.currentTarget.value;
  const previous = I18n.language;
  let saved = false;
  $$("#language, #login-language").forEach((select) => { select.disabled = true; });
  try {
    if (state.user) {
      const result = await api("/api/preferences", { method: "POST", body: JSON.stringify({ language }) });
      state.user = result.user;
    }
    saved = true;
    I18n.set(language);
    if (state.user) {
      $("#user-role").textContent = roleName(state.user.roles);
      showView(state.view);
      await refresh();
      if (state.view === "users") await loadUsers();
      showToast(t("Language saved."));
    }
  } catch (error) {
    if (!saved) I18n.set(previous);
    showToast(error.message);
  } finally { $$("#language, #login-language").forEach((select) => { select.disabled = false; }); }
}
$("#language").addEventListener("change", changeLanguage);
$("#login-language").addEventListener("change", changeLanguage);

async function openNotificationSettings() {
  setMenuOpen(false);
  if ($("#notification-modal").classList.contains("hidden")) openModal("notification-modal");
  $("#notification-error").textContent = "";
  $("#enable-notifications").disabled = true;
  $("#enable-notifications").classList.remove("hidden");
  $("#disable-notifications").classList.add("hidden");
  $("#test-notification").classList.add("hidden");
  const status = $("#notification-status");
  if (!window.isSecureContext || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    status.textContent = t("Push is unavailable here. Use HTTPS and a supported browser. On iPhone, open the installed Home Screen app.");
    return;
  }
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (ios && !navigator.standalone && !window.matchMedia("(display-mode: standalone)").matches) {
    status.textContent = t("Add FurnaceDesk to your Home Screen first, then open it from that icon.");
    return;
  }
  status.textContent = t("Checking this device...");
  try {
    await navigator.serviceWorker.register("/sw.js");
    pushRegistration = await navigator.serviceWorker.ready;
    pushSettings = await api("/api/push");
    const subscription = await pushRegistration.pushManager.getSubscription();
    const enabled = pushSettings.subscribed && !!subscription && Notification.permission === "granted";
    status.textContent = t(enabled ? "Notifications are enabled on this device." :
      Notification.permission === "denied" ? "Notifications are blocked. Allow them in your device or browser settings, then reopen this screen." :
        "Notifications are off on this device.");
    $("#enable-notifications").classList.toggle("hidden", enabled);
    $("#enable-notifications").disabled = Notification.permission === "denied";
    $("#disable-notifications").classList.toggle("hidden", !pushSettings.subscribed);
    $("#test-notification").classList.toggle("hidden", !enabled);
  } catch (error) {
    $("#notification-error").textContent = error.message;
    status.textContent = t("Could not check notification settings. Close this screen and try again.");
  }
}

$("#notifications-nav").addEventListener("click", openNotificationSettings);
$("#notification-shortcut").addEventListener("click", openNotificationSettings);
$("#enable-notifications").addEventListener("click", async () => {
  const button = $("#enable-notifications");
  button.disabled = true;
  $("#notification-error").textContent = "";
  try {
    // Must happen directly inside a user gesture for iOS permission prompts.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error(t("Permission was not granted. You can enable notifications later in device settings."));
    if (!pushRegistration || !pushSettings) throw new Error(t("Reopen notification settings and try again."));
    const key = Uint8Array.from(atob(pushSettings.public_key.replace(/-/g, "+").replace(/_/g, "/")), (char) => char.charCodeAt(0));
    const subscription = await pushRegistration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    await api("/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: subscription.toJSON() }) });
    await openNotificationSettings();
    showToast(t("Notifications enabled on this device."));
  } catch (error) {
    $("#notification-error").textContent = error.message;
  } finally { button.disabled = false; }
});
$("#disable-notifications").addEventListener("click", async () => {
  const button = $("#disable-notifications");
  button.disabled = true;
  $("#notification-error").textContent = "";
  try {
    await api("/api/push/unsubscribe", { method: "POST", body: "{}" });
    const subscription = await pushRegistration?.pushManager.getSubscription();
    if (subscription && !await subscription.unsubscribe()) {
      throw new Error(t("Server notifications are disabled, but the browser subscription could not be removed. Try again."));
    }
    await openNotificationSettings();
  } catch (error) { $("#notification-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#test-notification").addEventListener("click", async () => {
  const button = $("#test-notification");
  button.disabled = true;
  $("#notification-error").textContent = "";
  try {
    await api("/api/push/test", { method: "POST", body: "{}" });
    $("#notification-status").textContent = t("Test queued. If it does not arrive, check notification permissions, Focus mode, and server push-delivery logs.");
  } catch (error) { $("#notification-error").textContent = error.message; }
  finally { button.disabled = false; }
});

function openNotificationTicket() {
  const url = new URL(window.location.href);
  const ticketId = Number(url.searchParams.get("ticket"));
  if (!Number.isSafeInteger(ticketId) || ticketId <= 0) return;
  url.searchParams.delete("ticket");
  history.replaceState(null, "", url);
  const ticket = state.tickets.find((item) => item.id === ticketId);
  if (ticket) openTicket(ticketId);
  else showToast(t("This service is no longer assigned to you or is unavailable."));
}

async function refresh() {
  const [ticketData, technicianData, customerData] = await Promise.all([
    api("/api/tickets"),
    canManage() ? api("/api/technicians") : Promise.resolve({ users: [] }),
    canManage() ? api("/api/customers") : Promise.resolve({ customers: [] }),
  ]);
  state.tickets = ticketData.tickets;
  state.technicians = technicianData.users;
  state.customers = customerData.customers;
  renderBoard();
  renderDoneServices();
  renderCalendar();
  renderCustomerList();
  if (state.selectedCustomerId && state.view === "customers") await selectCustomer(state.selectedCustomerId);
}

function formatDate(value, options = { month: "short", day: "numeric" }) {
  if (!value) return "";
  const date = new Date(value);
  return new Intl.DateTimeFormat(I18n.locale, options).format(date);
}

function formatTime(value) {
  return value ? formatDate(value, { hour: "numeric", minute: "2-digit" }) : "";
}

function effortHours(value) {
  const match = String(value || "").trim().match(/^(\d+(?:\.\d+)?)\s*(h|d|w)$/i);
  if (!match) return 0;
  return Number(match[1]) * ({ h: 1, d: 8, w: 56 }[match[2].toLowerCase()]);
}

function formatHours(value) {
  return `${Number(value).toFixed(3).replace(/\.?0+$/, "")}h`;
}

function updateEffortPreview() {
  const value = $("#estimated-effort").value;
  const hours = effortHours(value);
  $("#effort-hours").textContent = hours > 0 ? t("Estimated: {hours}", { hours: formatHours(hours) }) :
    t(value.trim() ? "Use values such as 1h, 1.5d, or 1w" : "Enter an estimate to see hours");
}

function renderBoard() {
  const query = $("#ticket-search").value.trim().toLowerCase();
  const visible = state.tickets.filter((ticket) =>
    ticket.status !== "done" && [ticket.customer, ticket.phone, ticket.address, ticket.appliance_model, ticket.notes]
      .some((value) => String(value || "").toLowerCase().includes(query)));
  $("#ticket-count").textContent = t(visible.length === 1 ? "{count} service" : "{count} services", { count: visible.length });
  $("#board-tabs").innerHTML = statuses.map((status) => `
    <button type="button" role="tab" aria-selected="${state.boardStatus === status.key}"
      aria-controls="column-${status.key}" id="tab-${status.key}" data-board-status="${status.key}">
      <span class="status-dot ${status.key}"></span><span>${t(status.label)}</span>
      <span class="tab-count">${visible.filter((ticket) => ticket.status === status.key).length}</span>
    </button>`).join("");
  $$("[data-board-status]").forEach((button) => button.addEventListener("click", () => {
    state.boardStatus = button.dataset.boardStatus;
    renderBoard();
    $(`[data-board-status="${state.boardStatus}"]`).focus({ preventScroll: true });
  }));
  $("#board").innerHTML = statuses.map((status) => {
    const tickets = visible.filter((ticket) => ticket.status === status.key);
    return `<section class="kanban-column ${state.boardStatus === status.key ? "mobile-selected" : ""}" id="column-${status.key}" data-status="${status.key}" aria-label="${t(status.label)}">
      <div class="column-heading"><div class="column-title"><span class="status-dot ${status.key}"></span>${t(status.label)}</div><span class="column-count">${tickets.length}</span></div>
      <div class="ticket-list">${tickets.length ? tickets.map(ticketCard).join("") : `<div class="empty-column">${t("No services here yet")}</div>`}</div>
    </section>`;
  }).join("");
  $$(".ticket-card").forEach((card) => {
    card.querySelector(".ticket-open").addEventListener("click", () => {
      if (Date.now() >= (state.dragClickUntil || 0)) openTicket(Number(card.dataset.id));
    });
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", card.dataset.id);
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      setTimeout(() => { state.dragClickUntil = 0; }, 400);
    });
    card.querySelector(".ticket-move").addEventListener("change", async (event) => {
      const select = event.currentTarget;
      const ticket = state.tickets.find((item) => item.id === Number(card.dataset.id));
      select.disabled = true;
      try {
        await moveTicket(ticket.id, select.value);
      } catch (error) {
        select.value = ticket.status;
        select.disabled = false;
        showToast(error.message);
      }
    });
  });
  $$(".kanban-column").forEach((column) => {
    column.addEventListener("dragover", (event) => {
      event.preventDefault();
      column.classList.add("drag-over");
      event.dataTransfer.dropEffect = "move";
    });
    column.addEventListener("dragleave", (event) => {
      if (!column.contains(event.relatedTarget)) column.classList.remove("drag-over");
    });
    column.addEventListener("drop", async (event) => {
      event.preventDefault();
      column.classList.remove("drag-over");
      state.dragClickUntil = Date.now() + 600;
      const ticketId = Number(event.dataTransfer.getData("text/plain"));
      const ticket = state.tickets.find((item) => item.id === ticketId);
      const nextStatus = column.dataset.status;
      if (!ticket || ticket.status === nextStatus) return;
      try {
        await moveTicket(ticketId, nextStatus);
      } catch (error) {
        showToast(error.message);
      }
    });
  });
}

async function moveTicket(ticketId, nextStatus) {
  await api(`/api/tickets/${ticketId}`, { method: "PUT", body: JSON.stringify({ status: nextStatus }) });
  state.boardStatus = nextStatus;
  await refresh();
  showToast(t("Service moved to {status}.", { status: statusLabel(nextStatus) }));
}

function renderDoneServices() {
  const query = $("#done-search").value.trim().toLocaleLowerCase();
  const allDone = state.tickets.filter((ticket) => ticket.status === "done");
  const done = state.tickets.filter((ticket) => ticket.status === "done" &&
    [ticket.customer, ticket.phone, ticket.address, ticket.appliance_model, ticket.used_parts]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  done.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  $("#done-earnings").textContent = formatAmount(allDone.reduce((total, ticket) => total + ticket.earnings, 0));
  $("#done-parts-margin").textContent = formatAmount(allDone.reduce((total, ticket) => total + ticket.parts_margin, 0));
  $("#done-labor-transport").textContent = formatAmount(allDone.reduce((total, ticket) => total + ticket.labor_cost + ticket.transport_cost, 0));
  $("#done-count").textContent = t(done.length === 1 ? "{count} service" : "{count} services", { count: done.length });
  $("#done-list").innerHTML = done.length ? done.map((ticket) => `
    <button class="done-card" data-id="${ticket.id}">
      <span class="done-date">${esc(formatDate(ticket.updated_at, { year: "numeric", month: "short", day: "numeric" }))}</span>
      <span class="history-model">${esc(ticket.appliance_model)}</span>
      <span class="done-customer"><strong>${esc(ticket.customer)}</strong><span>${esc(ticket.phone)} · ${esc(ticket.address)}</span></span>
      <span class="done-total"><strong>${t("Earned {amount}", { amount: formatAmount(ticket.earnings) })}</strong><span>${t("Parts margin {margin} · Labor {labor} · Transport {transport}", { margin: formatAmount(ticket.parts_margin), labor: formatAmount(ticket.labor_cost), transport: formatAmount(ticket.transport_cost) })}</span></span>
      <span class="history-arrow">›</span>
    </button>`).join("") : `<div class="done-empty">${t("No completed services found.")}</div>`;
  $$(".done-card").forEach((card) => card.addEventListener("click", () =>
    openTicket(Number(card.dataset.id))));
}

function ticketCard(ticket) {
  const technician = state.technicians.find((item) => item.id === ticket.assignee_id);
  const appointment = ticket.appointment_start
    ? `<span class="appointment-chip">◷ ${esc(formatDate(ticket.appointment_start))}, ${esc(formatTime(ticket.appointment_start))}${ticket.estimated_hours ? ` · ${esc(formatHours(ticket.estimated_hours))}` : ""}</span>`
    : '<span class="appointment-chip"></span>';
  return `<article class="ticket-card" data-id="${ticket.id}" draggable="${!mobileLayout.matches}">
    <button class="ticket-open" type="button" aria-label="${esc(t("Open service for {customer}", { customer: ticket.customer }))}">
    <div class="ticket-card-top"><span class="model-tag">${esc(ticket.appliance_model)}</span><span class="ticket-id">#${ticket.id}</span></div>
    <h3 class="ticket-name">${esc(ticket.customer)}</h3>
    <p class="ticket-phone">${esc(ticket.phone)}</p>
    <p class="ticket-address">${esc(ticket.address)}</p>
    ${ticket.needed_parts ? `<div class="part-chip">${esc(t("Parts needed: {parts}", { parts: ticket.needed_parts }))}</div>` : ""}
    <div class="ticket-footer">${appointment}<span class="assignee-chip">${esc(technician?.username || "")}</span></div>
    </button>
    <div class="ticket-touch-actions">
      <a class="button button-quiet" href="tel:${esc(ticket.phone.replace(/[^\d+]/g, ""))}">${t("Call customer")}</a>
      <label class="ticket-move-label"><span>${t("Move to")}</span><select class="ticket-move" aria-label="${esc(t("Move service for {customer}", { customer: ticket.customer }))}">${statuses.map((status) => `<option value="${status.key}" ${ticket.status === status.key ? "selected" : ""}>${t(status.label)}</option>`).join("")}</select></label>
    </div>
  </article>`;
}

function renderCalendar() {
  const year = state.month.getFullYear();
  const month = state.month.getMonth();
  $("#calendar-month").textContent = new Intl.DateTimeFormat(I18n.locale, { month: "long", year: "numeric" }).format(state.month);
  const first = new Date(year, month, 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - mondayOffset);
  const dayCount = Math.ceil((mondayOffset + new Date(year, month + 1, 0).getDate()) / 7) * 7;
  const today = new Date();
  const days = [];
  for (let i = 0; i < dayCount; i++) {
    const day = new Date(start);
    day.setDate(start.getDate() + i);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    const events = state.tickets.filter((ticket) => ticket.status !== "done" && ticket.appointment_start?.slice(0, 10) === key);
    const classes = ["calendar-day", state.calendarDay === key ? "selected-day" : "", day.getMonth() !== month ? "outside" : "",
      day.toDateString() === today.toDateString() ? "today" : ""].filter(Boolean).join(" ");
    days.push(`<div class="${classes}" data-date="${key}" role="button" tabindex="0" aria-label="${t("{date}, {count} visits", { date: key, count: events.length })}" title="${t("Show visits on {date}", { date: key })}">
      <span class="day-number">${day.getDate()}</span>
      <span class="day-visit-count">${events.length ? t(events.length === 1 ? "{count} visit" : "{count} visits", { count: events.length }) : ""}</span>
      ${events.slice(0, 2).map((ticket) => `<div class="day-event ${ticket.status}" data-ticket-id="${ticket.id}">${esc(formatTime(ticket.appointment_start))} ${esc(ticket.customer)}</div>`).join("")}
      ${events.length > 2 ? `<div class="more-events">+${events.length - 2} more</div>` : ""}
    </div>`);
  }
  $("#calendar-days").innerHTML = days.join("");
  $$(".calendar-day").forEach((cell) => {
    cell.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); cell.click(); }
    });
    cell.addEventListener("click", (event) => {
      if (mobileLayout.matches) {
        state.calendarDay = cell.dataset.date;
        renderCalendar();
        return;
      }
      const eventEl = event.target.closest("[data-ticket-id]");
      if (eventEl) {
        event.stopPropagation();
        openTicket(Number(eventEl.dataset.ticketId));
        return;
      }
      if (canManage()) openTicket(null, `${cell.dataset.date}T09:00`);
    });
  });
  $("#agenda-heading").textContent = state.calendarDay ? formatDate(`${state.calendarDay}T12:00`, { month: "short", day: "numeric", weekday: "short" }) : t("Next visits");
  $("#agenda-eyebrow").textContent = t(state.calendarDay ? "SELECTED DAY" : "UPCOMING");
  $("#schedule-day").classList.toggle("hidden", !state.calendarDay || !canManage());
  const nextVisits = state.tickets
    .filter((ticket) => ticket.status !== "done" && ticket.appointment_start &&
      (state.calendarDay ? ticket.appointment_start.slice(0, 10) === state.calendarDay :
        new Date(ticket.appointment_start) >= new Date(new Date().setHours(0, 0, 0, 0))))
    .sort((a, b) => a.appointment_start.localeCompare(b.appointment_start)).slice(0, state.calendarDay ? undefined : 8);
  $("#agenda-list").innerHTML = nextVisits.length ? nextVisits.map((ticket) => `
    <button type="button" class="agenda-item" data-id="${ticket.id}">
      <p class="agenda-time">${esc(formatDate(ticket.appointment_start, { weekday: "short", month: "short", day: "numeric" }))} · ${esc(formatTime(ticket.appointment_start))}</p>
      <h3 class="agenda-customer">${esc(ticket.customer)}</h3>
      <p class="agenda-detail">${esc(ticket.appliance_model)} · ${esc(ticket.address)}${ticket.estimated_hours ? ` · ${esc(formatHours(ticket.estimated_hours))}` : ""}</p>
    </button>`).join("") : `<p class="agenda-empty">${t("No visits scheduled. Select a day to plan your next service.")}</p>`;
  $$(".agenda-item").forEach((item) => item.addEventListener("click", () => openTicket(Number(item.dataset.id))));
}

function showView(view) {
  state.view = view;
  $("#board-view").classList.toggle("hidden", view !== "board");
  $("#done-view").classList.toggle("hidden", view !== "done");
  $("#calendar-view").classList.toggle("hidden", view !== "calendar");
  $("#users-view").classList.toggle("hidden", view !== "users");
  $("#customers-view").classList.toggle("hidden", view !== "customers");
  $$("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("#page-title").textContent = t(view === "calendar" ? "Calendar" : view === "users" ? "Team members" :
    view === "customers" ? "Customers" : view === "done" ? "Done services" : "Service board");
  setMenuOpen(false);
  window.scrollTo({ top: 0 });
  if (view === "users") loadUsers();
  if (view === "customers") renderCustomerList();
}

function openModal(id) {
  const modal = $(`#${id}`);
  modal._returnFocus = document.activeElement;
  modal.classList.remove("hidden");
  if (id === "completion-modal") $("#ticket-modal").inert = true;
  document.body.style.overflow = "hidden";
  $(".modal-card", modal).scrollTop = 0;
  $(".modal-body", modal).scrollTop = 0;
  $("#app").inert = true;
  $("#toast").classList.remove("show");
  $(".modal-header button", modal).focus({ preventScroll: true });
}

function closeModal(id) {
  if (id === "ticket-modal") clearAddressSuggestions();
  $(`#${id}`).classList.add("hidden");
  if (id === "completion-modal") $("#ticket-modal").inert = false;
  $("#app").inert = $$(".modal:not(.hidden)").length > 0;
  document.body.style.overflow = $$(".modal:not(.hidden)").length ? "hidden" : "";
  $(`#${id}`)._returnFocus?.focus({ preventScroll: true });
}

function setMenuOpen(open) {
  const wasOpen = $("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#sidebar-backdrop").hidden = !open;
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
  $("#sidebar").inert = mobileLayout.matches && !open;
  $(".main-area").inert = mobileLayout.matches && open;
  $(".mobile-nav").inert = mobileLayout.matches && open;
  document.body.style.overflow = open || $$(".modal:not(.hidden)").length ? "hidden" : "";
  if (open) $("#menu-close").focus({ preventScroll: true });
  else if (wasOpen && mobileLayout.matches) $("#menu-toggle").focus({ preventScroll: true });
}

function syncDialogViewport() {
  const viewport = window.visualViewport;
  document.documentElement.style.setProperty("--dialog-height", `${viewport?.height ?? window.innerHeight}px`);
  document.documentElement.style.setProperty("--dialog-top", `${viewport?.offsetTop ?? 0}px`);
}

function openTicket(ticketId = null, appointment = "", customer = null) {
  clearAddressSuggestions();
  const form = $("#ticket-form");
  form.reset();
  $("#ticket-error").textContent = "";
  form.elements.id.value = ticketId || "";
  $("#ticket-modal-title").textContent = t(ticketId ? "Edit service" : "New service");
  form.elements.status.value = "received";
  $("#status-field").classList.toggle("hidden", !ticketId);
  form.elements.appointment_start.value = appointment;
  $("#effort-hours").textContent = t("Enter an estimate to see hours");
  form.elements.status.querySelector('option[value="done"]')?.remove();
  $("#customer-lookup").value = "";
  $("#customer-suggestions").classList.add("hidden");
  const ticket = state.tickets.find((item) => item.id === ticketId);
  if (ticket?.status === "done") {
    form.elements.status.insertAdjacentHTML("beforeend", `<option value="done">${t("Done")}</option>`);
  }
  if (ticket) {
    Object.entries(ticket).forEach(([key, value]) => {
      if (form.elements[key]) form.elements[key].value = value ?? "";
    });
    renderNeededPartRows(ticket.needed_parts_items || []);
  } else if (customer) {
    form.elements.customer.value = customer.name;
    form.elements.phone.value = customer.phone;
    form.elements.address.value = customer.address;
    $("#customer-lookup").value = `${customer.name} · ${customer.phone}`;
    renderNeededPartRows([]);
  } else renderNeededPartRows([]);
  updateEffortPreview();
  form.elements.assignee_id.innerHTML = `<option value="">${t("Unassigned")}</option>${state.technicians.map((person) =>
    `<option value="${person.id}">${esc(person.username)}</option>`).join("")}`;
  if (ticket) form.elements.assignee_id.value = ticket.assignee_id || "";
  else {
    const defaultTechnician = state.technicians.find((person) => person.username.toLowerCase() === "ognjen");
    if (defaultTechnician) form.elements.assignee_id.value = defaultTechnician.id;
  }
  const techLimited = hasRole("technician") && !canManage();
  for (const element of $$(".form-grid input, .form-grid select, .form-grid textarea", form)) {
    if (element.name === "id") continue;
    element.disabled = techLimited && !["status", "notes"].includes(element.name);
  }
  $$("#needed-parts-list input, #add-needed-part").forEach((element) => { element.disabled = techLimited; });
  $(".modal-footer button[type=submit]", form).textContent = t(techLimited ? "Save updates" : "Save service");
  $("#mark-done").classList.toggle("hidden", !ticket);
  $("#mark-done").textContent = t(ticket?.status === "done" ? "Edit completion costs" : "Mark service done");
  openModal("ticket-modal");
}

function clearAddressSuggestions() {
  clearTimeout(addressTimer);
  addressRequest?.abort();
  addressRequest = null;
  addressMatches = [];
  addressIndex = -1;
  $("#address-suggestions").innerHTML = "";
  $("#address-suggestions").classList.add("hidden");
  $("#service-address").setAttribute("aria-expanded", "false");
  $("#service-address").removeAttribute("aria-activedescendant");
  $("#address-status").textContent = "";
}

function selectAddress(index) {
  const match = addressMatches[index];
  if (!match) return;
  $("#service-address").value = match.label;
  clearAddressSuggestions();
  $("#service-address").focus({ preventScroll: true });
  $("#address-status").textContent = t("Address selected. You can still edit it.");
}

async function searchAddresses(query) {
  const request = new AbortController();
  addressRequest = request;
  $("#address-status").textContent = t("Searching addresses...");
  try {
    const result = await api("/api/addresses/search", {
      method: "POST", body: JSON.stringify({ query }), signal: request.signal,
    });
    if (request.signal.aborted || $("#service-address").value.trim() !== query ||
        $("#ticket-modal").classList.contains("hidden")) return;
    addressMatches = result.suggestions;
    addressIndex = -1;
    $("#address-suggestions").innerHTML = addressMatches.map((match, index) => `
      <button type="button" class="address-suggestion" role="option" tabindex="-1" id="address-option-${index}" data-address-index="${index}" aria-selected="false">
        <strong>${esc(match.label)}</strong><span>${esc(t(match.region))}${match.postcode ? ` · ${esc(match.postcode)}` : ""}${match.house_number && !match.house_number_verified ? ` · ${t("House number from your entry; please check.")}` : ""}</span>
      </button>`).join("");
    const hasMatches = addressMatches.length > 0;
    $("#address-suggestions").classList.toggle("hidden", !hasMatches);
    $("#service-address").setAttribute("aria-expanded", String(hasMatches));
    $("#address-status").textContent = t(hasMatches ? "Choose an address or keep typing." :
      "No matching streets found. Try a fuller street and town name, or enter the address manually.");
    $$("[data-address-index]").forEach((button) => {
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        selectAddress(Number(button.dataset.addressIndex));
      });
    });
  } catch (error) {
    if (error.name !== "AbortError" && !request.signal.aborted) {
      $("#address-status").textContent = t(error.message);
    }
  } finally {
    if (addressRequest === request) addressRequest = null;
  }
}

$("#service-address").addEventListener("input", (event) => {
  clearAddressSuggestions();
  const query = event.currentTarget.value.trim();
  if (query.length >= 3 && query.length <= 200 && canManage()) {
    addressTimer = setTimeout(() => searchAddresses(query), 700);
  }
});
$("#service-address").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    clearAddressSuggestions();
  } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && addressMatches.length) {
    event.preventDefault();
    addressIndex = addressIndex < 0 ? (event.key === "ArrowDown" ? 0 : addressMatches.length - 1) :
      (addressIndex + (event.key === "ArrowDown" ? 1 : -1) + addressMatches.length) % addressMatches.length;
    $$(".address-suggestion").forEach((button, index) => button.setAttribute("aria-selected", String(index === addressIndex)));
    $("#service-address").setAttribute("aria-activedescendant", `address-option-${addressIndex}`);
    $(`#address-option-${addressIndex}`).scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter" && addressIndex >= 0) {
    event.preventDefault();
    selectAddress(addressIndex);
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".address-field")) clearAddressSuggestions();
});

function renderCustomerList() {
  if (!canManage()) return;
  const query = $("#customer-search").value.trim().toLocaleLowerCase();
  const customers = state.customers.filter((customer) => customerMatches(customer, query));
  $("#customer-list").innerHTML = customers.length ? customers.map((customer) => `
    <button class="customer-card ${customer.id === state.selectedCustomerId ? "selected" : ""}" data-customer-id="${customer.id}">
      <span class="customer-avatar">${esc(customer.name.slice(0, 1).toUpperCase())}</span>
      <span class="customer-card-main"><strong>${esc(customer.name)}</strong><span>${esc(customer.phone)}</span></span>
      <span class="customer-service-count">${t(customer.service_count === 1 ? "{count} service" : "{count} services", { count: customer.service_count })}</span>
    </button>`).join("") : `<div class="customer-empty">${t("No customers found.")}</div>`;
  $$(".customer-card").forEach((card) => card.addEventListener("click", () =>
    selectCustomer(Number(card.dataset.customerId))));
}

async function selectCustomer(customerId) {
  state.selectedCustomerId = customerId;
  renderCustomerList();
  try {
    const { customer, services } = await api(`/api/customers/${customerId}`);
    if (state.selectedCustomerId !== customerId) return;
    $("#customer-detail").innerHTML = `
      <div class="customer-profile">
        <div class="customer-profile-heading">
          <div class="customer-avatar customer-avatar-large">${esc(customer.name.slice(0, 1).toUpperCase())}</div>
          <div><p class="eyebrow">${t("CUSTOMER PROFILE")}</p><h2>${esc(customer.name)}</h2><a class="customer-phone" href="tel:${esc(customer.phone)}">${esc(customer.phone)}</a></div>
          <button class="button button-primary customer-new-service" data-customer-new="${customer.id}">${t("＋ New service")}</button>
        </div>
        <p class="customer-profile-address">${esc(customer.address)}</p>
        <div class="customer-stats">${t(services.length === 1 ? "{count} service" : "{count} services", { count: services.length })} <span>·</span> ${t(new Set(services.map((service) => service.appliance_model)).size === 1 ? "{count} device" : "{count} devices", { count: new Set(services.map((service) => service.appliance_model)).size })}</div>
        <div class="history-heading"><div><p class="eyebrow">${t("CUSTOMER HISTORY")}</p><h3>${t("Previous services & devices")}</h3></div></div>
        <div class="service-history">${services.length ? services.map((service) => `
          <button class="history-item" data-history-ticket="${service.id}">
            <span class="history-model">${esc(service.appliance_model)}</span>
            <span class="history-main"><strong>${esc(formatDate(service.created_at))} · ${esc(service.status === "done" ? t("Done") : statusLabel(service.status))}</strong>
              <span>${esc(service.address)}</span>
              ${service.estimated_hours ? `<span>${esc(t("Estimated effort: {hours}", { hours: formatHours(service.estimated_hours) }))}</span>` : ""}
              ${service.parts.length ? `<span>${esc(t("Parts used: {parts}", { parts: service.parts.map((part) => t("{quantity} × {name} (customer {price}, cost {cost})", { quantity: part.quantity, name: part.name, price: formatAmount(part.regular_price), cost: formatAmount(part.discounted_cost) })).join(", ") }))}</span><span>${t("Parts margin: {amount}", { amount: formatAmount(service.parts_margin) })}</span>` : service.used_parts ? `<span>${esc(t("Parts used: {parts}", { parts: service.used_parts }))}</span>` : ""}
              ${service.needed_parts ? `<span>${esc(t("Parts needed: {parts}", { parts: service.needed_parts }))}</span>` : ""}
              ${service.status === "done" ? `<span>${t("Customer total: {amount}", { amount: formatAmount(service.customer_total) })}</span><span>${t("Earned: {amount} (parts margin {margin}, labor {labor}, transport {transport})", { amount: formatAmount(service.earnings), margin: formatAmount(service.parts_margin), labor: formatAmount(service.labor_cost), transport: formatAmount(service.transport_cost) })}</span>` : ""}
              ${service.notes ? `<span>${esc(service.notes)}</span>` : ""}
            </span>
            <span class="history-arrow">›</span>
          </button>`).join("") : `<p class="agenda-empty">${t("No previous services yet.")}</p>`}</div>
      </div>`;
    $(".customers-layout").classList.add("show-customer");
    $("#customer-back").classList.remove("hidden");
    if (mobileLayout.matches) $("#customer-back").scrollIntoView({ block: "start" });
    $("#customer-detail").querySelector("[data-customer-new]").addEventListener("click", () =>
      openTicket(null, "", customer));
    $$("[data-history-ticket]").forEach((button) => button.addEventListener("click", () =>
      openTicket(Number(button.dataset.historyTicket))));
  } catch (error) {
    showToast(error.message);
  }
}

function statusLabel(status) {
  return t(statuses.find((item) => item.key === status)?.label || "Problem");
}

function customerMatches(customer, query) {
  const nameMatches = customer.name.toLocaleLowerCase().includes(query);
  const phoneQuery = query.replace(/\D/g, "");
  const phoneMatches = phoneQuery && customer.phone.replace(/\D/g, "").includes(phoneQuery);
  return nameMatches || phoneMatches;
}

function formatAmount(value) {
  return new Intl.NumberFormat(I18n.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false }).format(Number(value || 0));
}

function searchCustomers(query) {
  const list = $("#customer-suggestions");
  const normalized = query.trim().toLocaleLowerCase();
  const matches = normalized.length >= 2 ? state.customers.filter((customer) =>
    customerMatches(customer, normalized)).slice(0, 6) : [];
  list.innerHTML = matches.map((customer) => `
    <button type="button" class="customer-suggestion" role="option" data-select-customer="${customer.id}">
      <strong>${esc(customer.name)}</strong><span>${esc(customer.phone)} · ${t(customer.service_count === 1 ? "{count} service" : "{count} services", { count: customer.service_count })}</span>
    </button>`).join("");
  list.classList.toggle("hidden", matches.length === 0);
  $$("[data-select-customer]", list).forEach((button) => button.addEventListener("click", () => {
    const customer = state.customers.find((item) => item.id === Number(button.dataset.selectCustomer));
    if (!customer) return;
    const form = $("#ticket-form");
    form.elements.customer.value = customer.name;
    form.elements.phone.value = customer.phone;
    form.elements.address.value = customer.address;
    $("#customer-lookup").value = `${customer.name} · ${customer.phone}`;
    list.classList.add("hidden");
  }));
}

function openUser(user = null) {
  const form = $("#user-form");
  form.reset();
  $("#user-error").textContent = "";
  form.elements.id.value = user?.id || "";
  $("#user-modal-title").textContent = t(user ? "Edit team member" : "Add team member");
  form.elements.username.value = user?.username || "";
  form.elements.password.required = !user;
  form.elements.password.value = "";
  form.elements.active.checked = user?.active ?? true;
  $("#active-field").classList.toggle("hidden", !user);
  $$('input[name="roles"]', form).forEach((checkbox) => checkbox.checked = Boolean(user?.roles.includes(checkbox.value)));
  openModal("user-modal");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2600);
}

async function loadUsers() {
  try {
    const data = await api("/api/users");
    state.users = data.users;
    $("#user-list").innerHTML = data.users.map((user) => `
      <div class="user-row">
        <strong>${esc(user.username)}${user.id === state.user.id ? ` ${t("(you)")}` : ""}</strong>
        <div class="role-pills">${user.roles.map((role) => `<span class="role-pill">${esc(roleName([role]))}</span>`).join("")}</div>
        <span class="account-state ${user.active ? "" : "inactive"}">${t(user.active ? "Active" : "Inactive")}</span>
        <button class="button button-quiet user-action" data-edit-user="${user.id}">${t("Edit")}</button>
      </div>`).join("");
    $$("[data-edit-user]").forEach((button) => button.addEventListener("click", () =>
      openUser(state.users.find((user) => user.id === Number(button.dataset.editUser)))));
  } catch (error) { showToast(error.message); }
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const form = new FormData(event.currentTarget);
  try {
    const result = await api("/api/login", { method: "POST", body: JSON.stringify({
      username: form.get("username"), password: form.get("password"),
    }) });
    showApp(result.user);
    await refresh();
    openNotificationTicket();
  } catch (error) { $("#login-error").textContent = error.message; }
});

function renderPartRows(parts) {
  $("#parts-list").innerHTML = (parts.length ? parts : [{}]).map((part) => `
    <div class="part-row">
      <label class="part-name-field"><span>${t("Part name")}</span><input class="part-name" aria-label="${t("Part name")}" maxlength="120" placeholder="${t("Part name")}" value="${esc(part.name || "")}"></label>
      <label class="part-quantity-field"><span>${t("Quantity")}</span><input class="part-quantity" aria-label="${t("Quantity")}" type="number" min="0" step="0.001" inputmode="decimal" placeholder="${t("Qty")}" value="${part.quantity ?? ""}"></label>
      <label class="part-price-field"><span>${t("Customer price / unit")}</span><input class="part-price" aria-label="${t("Customer price per unit")}" type="number" min="0" max="100000000" step="0.01" inputmode="decimal" placeholder="0.00" value="${part.regular_price ?? ""}"></label>
      <label class="part-cost-field"><span>${t("Your cost / unit")}</span><input class="part-cost" aria-label="${t("Your cost per unit")}" type="number" min="0" max="100000000" step="0.01" inputmode="decimal" placeholder="0.00" value="${part.discounted_cost ?? ""}"></label>
      <button class="icon-button remove-part" type="button" aria-label="${t("Remove part")}">×</button>
    </div>`).join("");
  $$(".part-row").forEach((row) => row.querySelectorAll("input").forEach((input) =>
    input.addEventListener("input", updateCompletionTotal)));
  $$(".remove-part").forEach((button) => button.addEventListener("click", () => {
    button.closest(".part-row").remove();
    updateCompletionTotal();
  }));
  updateCompletionTotal();
}

function renderNeededPartRows(parts) {
  $("#needed-parts-list").innerHTML = (parts.length ? parts : [{}]).map((part) => `
    <div class="needed-part-row">
      <input class="needed-part-name" aria-label="${t("Needed part name")}" maxlength="120" placeholder="${t("Part name")}" value="${esc(part.name || "")}">
      <input class="needed-part-quantity" aria-label="${t("Needed part quantity")}" type="number" min="0" step="0.001" inputmode="decimal" placeholder="${t("Quantity")}" value="${part.quantity ?? ""}">
      <button class="icon-button remove-needed-part" type="button" aria-label="${t("Remove needed part")}">×</button>
    </div>`).join("");
  $$(".remove-needed-part").forEach((button) => button.addEventListener("click", () => {
    button.closest(".needed-part-row").remove();
  }));
}

function updateCompletionTotal() {
  const partsTotal = $$(".part-row").reduce((total, row) => {
    const quantity = Number(row.querySelector(".part-quantity").value) || 0;
    const price = Number(row.querySelector(".part-price").value) || 0;
    return total + quantity * price;
  }, 0);
  const labor = Number($("#completion-form").elements.labor_cost.value) || 0;
  const transport = Number($("#completion-form").elements.transport_cost.value) || 0;
  $("#completion-total").textContent = formatAmount(partsTotal + labor + transport);
  const partsCost = $$(".part-row").reduce((total, row) => {
    const quantity = Number(row.querySelector(".part-quantity").value) || 0;
    const cost = Number(row.querySelector(".part-cost").value) || 0;
    return total + quantity * cost;
  }, 0);
  $("#completion-earnings").textContent = formatAmount(partsTotal - partsCost + labor + transport);
}

function openCompletion(ticketId) {
  const ticket = state.tickets.find((item) => item.id === ticketId);
  if (!ticket) return;
  const form = $("#completion-form");
  form.reset();
  $("#completion-error").textContent = "";
  form.elements.ticket_id.value = ticket.id;
  form.elements.labor_cost.value = ticket.labor_cost || 0;
  form.elements.transport_cost.value = ticket.transport_cost || 0;
  $("#completion-modal-title").textContent = t(ticket.status === "done" ? "Edit service costs" : "Complete service");
  $(".modal-footer button[type=submit]", form).textContent = t(ticket.status === "done" ? "Save costs" : "Save & mark done");
  renderPartRows(ticket.status === "done" ? ticket.parts || [] : ticket.needed_parts_items || []);
  openModal("completion-modal");
}

$("#completion-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#completion-error").textContent = "";
  const form = event.currentTarget;
  const parts = $$(".part-row").map((row) => ({
    name: row.querySelector(".part-name").value,
    quantity: row.querySelector(".part-quantity").value,
    regular_price: row.querySelector(".part-price").value,
    discounted_cost: row.querySelector(".part-cost").value,
  }));
  const ticketId = form.elements.ticket_id.value;
  const submit = $('[type="submit"]', form);
  submit.disabled = true;
  try {
    await api(`/api/tickets/${ticketId}/complete`, {
      method: "POST",
      body: JSON.stringify({
        parts,
        labor_cost: Number(form.elements.labor_cost.value) || 0,
        transport_cost: Number(form.elements.transport_cost.value) || 0,
      }),
    });
    closeModal("completion-modal");
    closeModal("ticket-modal");
    await refresh();
    showToast(t("Service costs saved. Service marked done."));
  } catch (error) {
    $("#completion-error").textContent = error.message;
    $("#completion-error").scrollIntoView({ block: "nearest" });
  } finally {
    submit.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } finally { showLogin(); }
});

$("#ticket-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#ticket-error").textContent = "";
  const form = new FormData(event.currentTarget);
  const id = form.get("id");
  const techLimited = hasRole("technician") && !canManage();
  const fields = techLimited
    ? ["status", "notes"]
    : ["customer", "phone", "address", "appliance_model", "status", "notes",
      "appointment_start", "estimated_effort"];
  const payload = Object.fromEntries(fields.map((key) => [key, form.get(key)]));
  if (!techLimited) payload.needed_parts_items = $$(".needed-part-row").map((row) => ({
    name: row.querySelector(".needed-part-name").value,
    quantity: row.querySelector(".needed-part-quantity").value,
  }));
  if (!id) payload.status = "received";
  if (!techLimited) payload.assignee_id = form.get("assignee_id");
  const submit = $('[type="submit"]', event.currentTarget);
  submit.disabled = true;
  try {
    await api(id ? `/api/tickets/${id}` : "/api/tickets", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    if (payload.status !== "done") state.boardStatus = payload.status;
    closeModal("ticket-modal");
    await refresh();
    showToast(t(id ? "Service updated." : "Service created."));
  } catch (error) {
    $("#ticket-error").textContent = error.message;
    $("#ticket-error").scrollIntoView({ block: "nearest" });
  } finally { submit.disabled = false; }
});

$("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#user-error").textContent = "";
  const form = new FormData(event.currentTarget);
  const id = form.get("id");
  const payload = {
    username: form.get("username"),
    password: form.get("password"),
    roles: form.getAll("roles"),
  };
  if (id) payload.active = form.get("active") === "on";
  const submit = $('[type="submit"]', event.currentTarget);
  submit.disabled = true;
  try {
    await api(id ? `/api/users/${id}` : "/api/users", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    closeModal("user-modal");
    await loadUsers();
    const technicians = await api("/api/technicians");
    state.technicians = technicians.users;
    renderBoard();
    showToast(t(id ? "Team member updated." : "Team member added."));
  } catch (error) {
    $("#user-error").textContent = error.message;
    $("#user-error").scrollIntoView({ block: "nearest" });
  } finally { submit.disabled = false; }
});

$("#new-ticket").addEventListener("click", () =>
  openTicket(null, state.view === "calendar" && state.calendarDay ? `${state.calendarDay}T09:00` : ""));
$("#add-user").addEventListener("click", () => openUser());
$("#users-nav").addEventListener("click", () => showView("users"));
$("#customers-nav").addEventListener("click", () => showView("customers"));
$("#ticket-search").addEventListener("input", renderBoard);
$("#done-search").addEventListener("input", renderDoneServices);
$("#customer-search").addEventListener("input", renderCustomerList);
$("#customer-back").addEventListener("click", () => {
  $(".customers-layout").classList.remove("show-customer");
  $("#customer-back").classList.add("hidden");
  state.selectedCustomerId = null;
  $("#customer-search").focus({ preventScroll: true });
});
$("#estimated-effort").addEventListener("input", updateEffortPreview);
$("#customer-lookup").addEventListener("input", (event) => searchCustomers(event.currentTarget.value));
$("#customer-lookup").addEventListener("focus", (event) => searchCustomers(event.currentTarget.value));
$("#customer-lookup").addEventListener("keydown", (event) => {
  if (event.key === "Escape") $("#customer-suggestions").classList.add("hidden");
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".customer-lookup-wrap")) $("#customer-suggestions").classList.add("hidden");
});
$("#mark-done").addEventListener("click", () => {
  openCompletion(Number($("#ticket-form").elements.id.value));
});
$("#add-part").addEventListener("click", () => {
  renderPartRows([
    ...$$(".part-row").map((row) => ({
      name: row.querySelector(".part-name").value,
      quantity: row.querySelector(".part-quantity").value,
      regular_price: row.querySelector(".part-price").value,
      discounted_cost: row.querySelector(".part-cost").value,
    })),
    {},
  ]);
  $$(".part-name").at(-1).focus();
});
$("#add-needed-part").addEventListener("click", () => {
  renderNeededPartRows([
    ...$$(".needed-part-row").map((row) => ({
      name: row.querySelector(".needed-part-name").value,
      quantity: row.querySelector(".needed-part-quantity").value,
    })),
    {},
  ]);
  $$(".needed-part-name").at(-1).focus();
});
$("#completion-form").elements.labor_cost.addEventListener("input", updateCompletionTotal);
$("#completion-form").elements.transport_cost.addEventListener("input", updateCompletionTotal);
$("#calendar-prev").addEventListener("click", () => { state.calendarDay = null; state.month.setDate(1); state.month.setMonth(state.month.getMonth() - 1); renderCalendar(); });
$("#calendar-next").addEventListener("click", () => { state.calendarDay = null; state.month.setDate(1); state.month.setMonth(state.month.getMonth() + 1); renderCalendar(); });
$("#schedule-day").addEventListener("click", () => openTicket(null, `${state.calendarDay}T09:00`));
$("#menu-toggle").addEventListener("click", () => setMenuOpen(!$("#sidebar").classList.contains("open")));
$("#menu-close").addEventListener("click", () => setMenuOpen(false));
$("#sidebar-backdrop").addEventListener("click", () => setMenuOpen(false));
$$("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$("[data-close]").forEach((button) => button.addEventListener("click", () => closeModal(button.dataset.close)));
$("#sync-calendar")?.addEventListener("click", () => { window.location.href = "/calendar.ics"; });
document.addEventListener("keydown", (event) => {
  const modals = $$(".modal:not(.hidden)");
  const modal = modals.at(-1);
  const focusRoot = modal || ($("#sidebar").classList.contains("open") ? $("#sidebar") : null);
  if (event.key === "Escape") {
    if (modal) closeModal(modal.id);
    else setMenuOpen(false);
  }
  if (event.key === "Tab" && focusRoot) {
    const focusable = $$('button, input, select, textarea, a[href]', focusRoot)
      .filter((element) => !element.disabled && element.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
mobileLayout.addEventListener("change", () => {
  if (state.user) { renderBoard(); renderCalendar(); }
  setMenuOpen(false);
});
window.visualViewport?.addEventListener("resize", syncDialogViewport);
window.visualViewport?.addEventListener("scroll", syncDialogViewport);
window.addEventListener("resize", syncDialogViewport);
syncDialogViewport();

(async function init() {
  try {
    const result = await api("/api/me");
    showApp(result.user);
    await refresh();
    openNotificationTicket();
  } catch {
    showLogin();
  }
})();
