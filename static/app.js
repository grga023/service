const state = {
  user: null, tickets: [], technicians: [], users: [], customers: [],
  selectedCustomerId: null, view: "board", month: new Date(),
};
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
}[role])).join(" · ");

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
  });
  if (response.status === 204) return null;
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && url !== "/api/login") showLogin();
    throw new Error(result.error || "Something went wrong.");
  }
  return result;
}

function showLogin() {
  state.user = null;
  $("#app").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
}

function showApp(user) {
  state.user = user;
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-name").textContent = user.username;
  $("#user-role").textContent = roleName(user.roles);
  $("#user-avatar").textContent = user.username.slice(0, 1).toUpperCase();
  $("#users-nav").classList.toggle("hidden", !hasRole("admin"));
  $("#customers-nav").classList.toggle("hidden", !canManage());
  $$(".calendar-export").forEach((link) => link.classList.toggle("hidden", !user));
  $("#new-ticket").classList.toggle("hidden", !canManage());
  $("#assignee-field").classList.toggle("hidden", !canManage());
  $("#customer-lookup-field").classList.toggle("hidden", !canManage());
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
  return new Intl.DateTimeFormat("en", options).format(date);
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
  $("#effort-hours").textContent = hours > 0 ? `Estimated: ${formatHours(hours)}` :
    value.trim() ? "Use values such as 1h, 1.5d, or 1w" : "Enter an estimate to see hours";
}

function renderBoard() {
  const query = $("#ticket-search").value.trim().toLowerCase();
  const visible = state.tickets.filter((ticket) =>
    ticket.status !== "done" && [ticket.customer, ticket.phone, ticket.address, ticket.appliance_model, ticket.notes]
      .some((value) => String(value || "").toLowerCase().includes(query)));
  $("#ticket-count").textContent = `${visible.length} ${visible.length === 1 ? "service" : "services"}`;
  $("#board").innerHTML = statuses.map((status) => {
    const tickets = visible.filter((ticket) => ticket.status === status.key);
    return `<section class="kanban-column" data-status="${status.key}">
      <div class="column-heading"><div class="column-title"><span class="status-dot ${status.key}"></span>${status.label}</div><span class="column-count">${tickets.length}</span></div>
      <div class="ticket-list">${tickets.length ? tickets.map(ticketCard).join("") : '<div class="empty-column">No services here yet</div>'}</div>
    </section>`;
  }).join("");
  $$(".ticket-card").forEach((card) => {
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", card.dataset.id);
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      setTimeout(() => { state.dragClickUntil = 0; }, 400);
    });
    card.addEventListener("click", () => {
      if (Date.now() < (state.dragClickUntil || 0)) return;
      openTicket(Number(card.dataset.id));
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
        await api(`/api/tickets/${ticketId}`, { method: "PUT", body: JSON.stringify({ status: nextStatus }) });
        await refresh();
        showToast(`Service moved to ${statusLabel(nextStatus)}.`);
      } catch (error) {
        showToast(error.message);
      }
    });
  });
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
  $("#done-count").textContent = `${done.length} ${done.length === 1 ? "service" : "services"}`;
  $("#done-list").innerHTML = done.length ? done.map((ticket) => `
    <button class="done-card" data-id="${ticket.id}">
      <span class="done-date">${esc(formatDate(ticket.updated_at, { year: "numeric", month: "short", day: "numeric" }))}</span>
      <span class="history-model">${esc(ticket.appliance_model)}</span>
      <span class="done-customer"><strong>${esc(ticket.customer)}</strong><span>${esc(ticket.phone)} · ${esc(ticket.address)}</span></span>
      <span class="done-total"><strong>Earned ${formatAmount(ticket.earnings)}</strong><span>Parts margin ${formatAmount(ticket.parts_margin)} · Labor ${formatAmount(ticket.labor_cost)} · Transport ${formatAmount(ticket.transport_cost)}</span></span>
      <span class="history-arrow">›</span>
    </button>`).join("") : '<div class="done-empty">No completed services found.</div>';
  $$(".done-card").forEach((card) => card.addEventListener("click", () =>
    openTicket(Number(card.dataset.id))));
}

function ticketCard(ticket) {
  const technician = state.technicians.find((item) => item.id === ticket.assignee_id);
  const appointment = ticket.appointment_start
    ? `<span class="appointment-chip">◷ ${esc(formatDate(ticket.appointment_start))}, ${esc(formatTime(ticket.appointment_start))}${ticket.estimated_hours ? ` · ${esc(formatHours(ticket.estimated_hours))}` : ""}</span>`
    : '<span class="appointment-chip"></span>';
  return `<article class="ticket-card" data-id="${ticket.id}" draggable="true">
    <div class="ticket-card-top"><span class="model-tag">${esc(ticket.appliance_model)}</span><span class="ticket-id">#${ticket.id}</span></div>
    <h3 class="ticket-name">${esc(ticket.customer)}</h3>
    <p class="ticket-phone">${esc(ticket.phone)}</p>
    <p class="ticket-address">${esc(ticket.address)}</p>
    ${ticket.needed_parts ? `<div class="part-chip">Parts needed: ${esc(ticket.needed_parts)}</div>` : ""}
    <div class="ticket-footer">${appointment}<span class="assignee-chip">${esc(technician?.username || "")}</span></div>
  </article>`;
}

function renderCalendar() {
  const year = state.month.getFullYear();
  const month = state.month.getMonth();
  $("#calendar-month").textContent = new Intl.DateTimeFormat("en", { month: "long", year: "numeric" }).format(state.month);
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
    const classes = ["calendar-day", day.getMonth() !== month ? "outside" : "",
      day.toDateString() === today.toDateString() ? "today" : ""].filter(Boolean).join(" ");
    days.push(`<div class="${classes}" data-date="${key}" title="Add service on ${key}">
      <span class="day-number">${day.getDate()}</span>
      ${events.slice(0, 2).map((ticket) => `<div class="day-event ${ticket.status}" data-ticket-id="${ticket.id}">${esc(formatTime(ticket.appointment_start))} ${esc(ticket.customer)}</div>`).join("")}
      ${events.length > 2 ? `<div class="more-events">+${events.length - 2} more</div>` : ""}
    </div>`);
  }
  $("#calendar-days").innerHTML = days.join("");
  $$(".calendar-day").forEach((cell) => cell.addEventListener("click", (event) => {
    const eventEl = event.target.closest("[data-ticket-id]");
    if (eventEl) {
      event.stopPropagation();
      openTicket(Number(eventEl.dataset.ticketId));
      return;
    }
    if (canManage()) openTicket(null, `${cell.dataset.date}T09:00`);
  }));
  const nextVisits = state.tickets
    .filter((ticket) => ticket.status !== "done" && ticket.appointment_start && new Date(ticket.appointment_start) >= new Date(new Date().setHours(0, 0, 0, 0)))
    .sort((a, b) => a.appointment_start.localeCompare(b.appointment_start)).slice(0, 8);
  $("#agenda-list").innerHTML = nextVisits.length ? nextVisits.map((ticket) => `
    <article class="agenda-item" data-id="${ticket.id}">
      <p class="agenda-time">${esc(formatDate(ticket.appointment_start, { weekday: "short", month: "short", day: "numeric" }))} · ${esc(formatTime(ticket.appointment_start))}</p>
      <h3 class="agenda-customer">${esc(ticket.customer)}</h3>
      <p class="agenda-detail">${esc(ticket.appliance_model)} · ${esc(ticket.address)}${ticket.estimated_hours ? ` · ${esc(formatHours(ticket.estimated_hours))}` : ""}</p>
    </article>`).join("") : '<p class="agenda-empty">No upcoming visits. Click a day on the calendar to schedule a service.</p>';
  $$(".agenda-item").forEach((item) => item.addEventListener("click", () => openTicket(Number(item.dataset.id))));
}

function showView(view) {
  state.view = view;
  $("#board-view").classList.toggle("hidden", view !== "board");
  $("#done-view").classList.toggle("hidden", view !== "done");
  $("#calendar-view").classList.toggle("hidden", view !== "calendar");
  $("#users-view").classList.toggle("hidden", view !== "users");
  $("#customers-view").classList.toggle("hidden", view !== "customers");
  $$(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#page-title").textContent = view === "calendar" ? "Calendar" : view === "users" ? "Team members" :
    view === "customers" ? "Customers" : view === "done" ? "Done services" : "Service board";
  $("#sidebar").classList.remove("open");
  if (view === "users") loadUsers();
  if (view === "customers") renderCustomerList();
}

function openModal(id) {
  $(`#${id}`).classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeModal(id) {
  $(`#${id}`).classList.add("hidden");
  document.body.style.overflow = $$(".modal:not(.hidden)").length ? "hidden" : "";
}

function openTicket(ticketId = null, appointment = "", customer = null) {
  const form = $("#ticket-form");
  form.reset();
  $("#ticket-error").textContent = "";
  form.elements.id.value = ticketId || "";
  $("#ticket-modal-title").textContent = ticketId ? "Edit service" : "New service";
  form.elements.status.value = "received";
  $("#status-field").classList.toggle("hidden", !ticketId);
  form.elements.appointment_start.value = appointment;
  $("#effort-hours").textContent = "Enter an estimate to see hours";
  form.elements.status.querySelector('option[value="done"]')?.remove();
  $("#customer-lookup").value = "";
  $("#customer-suggestions").classList.add("hidden");
  const ticket = state.tickets.find((item) => item.id === ticketId);
  if (ticket?.status === "done") {
    form.elements.status.insertAdjacentHTML("beforeend", '<option value="done">Done</option>');
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
  form.elements.assignee_id.innerHTML = `<option value="">Unassigned</option>${state.technicians.map((person) =>
    `<option value="${person.id}">${esc(person.username)}</option>`).join("")}`;
  if (ticket?.assignee_id) form.elements.assignee_id.value = ticket.assignee_id;
  const techLimited = hasRole("technician") && !canManage();
  for (const element of $$(".form-grid input, .form-grid select, .form-grid textarea", form)) {
    if (element.name === "id") continue;
    element.disabled = techLimited && !["status", "notes"].includes(element.name);
  }
  $$("#needed-parts-list input, #add-needed-part").forEach((element) => { element.disabled = techLimited; });
  $(".modal-footer button[type=submit]", form).textContent = techLimited ? "Save updates" : "Save service";
  $("#mark-done").classList.toggle("hidden", !ticket);
  $("#mark-done").textContent = ticket?.status === "done" ? "Edit completion costs" : "Mark service done";
  openModal("ticket-modal");
}

function renderCustomerList() {
  if (!canManage()) return;
  const query = $("#customer-search").value.trim().toLocaleLowerCase();
  const customers = state.customers.filter((customer) => customerMatches(customer, query));
  $("#customer-list").innerHTML = customers.length ? customers.map((customer) => `
    <button class="customer-card ${customer.id === state.selectedCustomerId ? "selected" : ""}" data-customer-id="${customer.id}">
      <span class="customer-avatar">${esc(customer.name.slice(0, 1).toUpperCase())}</span>
      <span class="customer-card-main"><strong>${esc(customer.name)}</strong><span>${esc(customer.phone)}</span></span>
      <span class="customer-service-count">${customer.service_count} ${customer.service_count === 1 ? "service" : "services"}</span>
    </button>`).join("") : '<div class="customer-empty">No customers found.</div>';
  $$(".customer-card").forEach((card) => card.addEventListener("click", () =>
    selectCustomer(Number(card.dataset.customerId))));
}

async function selectCustomer(customerId) {
  state.selectedCustomerId = customerId;
  renderCustomerList();
  try {
    const { customer, services } = await api(`/api/customers/${customerId}`);
    $("#customer-detail").innerHTML = `
      <div class="customer-profile">
        <div class="customer-profile-heading">
          <div class="customer-avatar customer-avatar-large">${esc(customer.name.slice(0, 1).toUpperCase())}</div>
          <div><p class="eyebrow">CUSTOMER PROFILE</p><h2>${esc(customer.name)}</h2><a class="customer-phone" href="tel:${esc(customer.phone)}">${esc(customer.phone)}</a></div>
          <button class="button button-primary customer-new-service" data-customer-new="${customer.id}">＋ New service</button>
        </div>
        <p class="customer-profile-address">${esc(customer.address)}</p>
        <div class="customer-stats">${services.length} ${services.length === 1 ? "service" : "services"} <span>·</span> ${[...new Set(services.map((service) => service.appliance_model))].length} ${new Set(services.map((service) => service.appliance_model)).size === 1 ? "device" : "devices"}</div>
        <div class="history-heading"><div><p class="eyebrow">CUSTOMER HISTORY</p><h3>Previous services &amp; devices</h3></div></div>
        <div class="service-history">${services.length ? services.map((service) => `
          <button class="history-item" data-history-ticket="${service.id}">
            <span class="history-model">${esc(service.appliance_model)}</span>
            <span class="history-main"><strong>${esc(formatDate(service.created_at))} · ${esc(service.status === "done" ? "Done" : statusLabel(service.status))}</strong>
              <span>${esc(service.address)}</span>
              ${service.estimated_hours ? `<span>Estimated effort: ${esc(formatHours(service.estimated_hours))}</span>` : ""}
              ${service.parts.length ? `<span>Parts used: ${esc(service.parts.map((part) => `${part.quantity} × ${part.name} (customer ${formatAmount(part.regular_price)}, cost ${formatAmount(part.discounted_cost)})`).join(", "))}</span><span>Parts margin: ${formatAmount(service.parts_margin)}</span>` : service.used_parts ? `<span>Parts used: ${esc(service.used_parts)}</span>` : ""}
              ${service.needed_parts ? `<span>Parts needed: ${esc(service.needed_parts)}</span>` : ""}
              ${service.status === "done" ? `<span>Customer total: ${formatAmount(service.customer_total)}</span><span>Earned: ${formatAmount(service.earnings)} (parts margin ${formatAmount(service.parts_margin)}, labor ${formatAmount(service.labor_cost)}, transport ${formatAmount(service.transport_cost)})</span>` : ""}
              ${service.notes ? `<span>${esc(service.notes)}</span>` : ""}
            </span>
            <span class="history-arrow">›</span>
          </button>`).join("") : '<p class="agenda-empty">No previous services yet.</p>'}</div>
      </div>`;
    $("#customer-detail").querySelector("[data-customer-new]").addEventListener("click", () =>
      openTicket(null, "", customer));
    $$("[data-history-ticket]").forEach((button) => button.addEventListener("click", () =>
      openTicket(Number(button.dataset.historyTicket))));
  } catch (error) {
    showToast(error.message);
  }
}

function statusLabel(status) {
  return statuses.find((item) => item.key === status)?.label || "Problem";
}

function customerMatches(customer, query) {
  const nameMatches = customer.name.toLocaleLowerCase().includes(query);
  const phoneQuery = query.replace(/\D/g, "");
  const phoneMatches = phoneQuery && customer.phone.replace(/\D/g, "").includes(phoneQuery);
  return nameMatches || phoneMatches;
}

function formatAmount(value) {
  return Number(value || 0).toFixed(2);
}

function searchCustomers(query) {
  const list = $("#customer-suggestions");
  const normalized = query.trim().toLocaleLowerCase();
  const matches = normalized.length >= 2 ? state.customers.filter((customer) =>
    customerMatches(customer, normalized)).slice(0, 6) : [];
  list.innerHTML = matches.map((customer) => `
    <button type="button" class="customer-suggestion" role="option" data-select-customer="${customer.id}">
      <strong>${esc(customer.name)}</strong><span>${esc(customer.phone)} · ${customer.service_count} ${customer.service_count === 1 ? "service" : "services"}</span>
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
  $("#user-modal-title").textContent = user ? "Edit team member" : "Add team member";
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
        <strong>${esc(user.username)}${user.id === state.user.id ? " (you)" : ""}</strong>
        <div class="role-pills">${user.roles.map((role) => `<span class="role-pill">${esc(roleName([role]))}</span>`).join("")}</div>
        <span class="account-state ${user.active ? "" : "inactive"}">${user.active ? "Active" : "Inactive"}</span>
        <button class="button button-quiet user-action" data-edit-user="${user.id}">Edit</button>
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
  } catch (error) { $("#login-error").textContent = error.message; }
});

function renderPartRows(parts) {
  $("#parts-list").innerHTML = (parts.length ? parts : [{}]).map((part) => `
    <div class="part-row">
      <input class="part-name" aria-label="Part name" maxlength="120" placeholder="Part name" value="${esc(part.name || "")}">
      <input class="part-quantity" aria-label="Quantity" type="number" min="0" step="0.001" inputmode="decimal" placeholder="Qty" value="${part.quantity ?? ""}">
      <input class="part-price" aria-label="Regular price charged to customer per part" type="number" min="0" max="100000000" step="0.01" inputmode="decimal" placeholder="Customer price" value="${part.regular_price ?? ""}">
      <input class="part-cost" aria-label="Your discounted purchase cost per part" type="number" min="0" max="100000000" step="0.01" inputmode="decimal" placeholder="Your cost" value="${part.discounted_cost ?? ""}">
      <button class="icon-button remove-part" type="button" aria-label="Remove part">×</button>
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
      <input class="needed-part-name" aria-label="Needed part name" maxlength="120" placeholder="Part name" value="${esc(part.name || "")}">
      <input class="needed-part-quantity" aria-label="Needed part quantity" type="number" min="0" step="0.001" inputmode="decimal" placeholder="Quantity" value="${part.quantity ?? ""}">
      <button class="icon-button remove-needed-part" type="button" aria-label="Remove needed part">×</button>
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
  $("#completion-modal-title").textContent = ticket.status === "done" ? "Edit service costs" : "Complete service";
  $(".modal-footer button[type=submit]", form).textContent = ticket.status === "done" ? "Save costs" : "Save & mark done";
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
    showToast("Service costs saved. Service marked done.");
  } catch (error) {
    $("#completion-error").textContent = error.message;
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
  try {
    await api(id ? `/api/tickets/${id}` : "/api/tickets", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    closeModal("ticket-modal");
    await refresh();
    showToast(id ? "Service updated." : "Service created.");
  } catch (error) { $("#ticket-error").textContent = error.message; }
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
  try {
    await api(id ? `/api/users/${id}` : "/api/users", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    closeModal("user-modal");
    await loadUsers();
    const technicians = await api("/api/technicians");
    state.technicians = technicians.users;
    renderBoard();
    showToast(id ? "Team member updated." : "Team member added.");
  } catch (error) { $("#user-error").textContent = error.message; }
});

$("#new-ticket").addEventListener("click", () => openTicket());
$("#add-user").addEventListener("click", () => openUser());
$("#users-nav").addEventListener("click", () => showView("users"));
$("#customers-nav").addEventListener("click", () => showView("customers"));
$("#ticket-search").addEventListener("input", renderBoard);
$("#done-search").addEventListener("input", renderDoneServices);
$("#customer-search").addEventListener("input", renderCustomerList);
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
$("#add-part").addEventListener("click", () => renderPartRows([
  ...$$(".part-row").map((row) => ({
    name: row.querySelector(".part-name").value,
    quantity: row.querySelector(".part-quantity").value,
    regular_price: row.querySelector(".part-price").value,
    discounted_cost: row.querySelector(".part-cost").value,
  })),
  {},
]));
$("#add-needed-part").addEventListener("click", () => renderNeededPartRows([
  ...$$(".needed-part-row").map((row) => ({
    name: row.querySelector(".needed-part-name").value,
    quantity: row.querySelector(".needed-part-quantity").value,
  })),
  {},
]));
$("#completion-form").elements.labor_cost.addEventListener("input", updateCompletionTotal);
$("#completion-form").elements.transport_cost.addEventListener("input", updateCompletionTotal);
$("#calendar-prev").addEventListener("click", () => { state.month.setDate(1); state.month.setMonth(state.month.getMonth() - 1); renderCalendar(); });
$("#calendar-next").addEventListener("click", () => { state.month.setDate(1); state.month.setMonth(state.month.getMonth() + 1); renderCalendar(); });
$("#menu-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$$(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$$("[data-close]").forEach((button) => button.addEventListener("click", () => closeModal(button.dataset.close)));
$("#sync-calendar")?.addEventListener("click", () => { window.location.href = "/calendar.ics"; });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") $$(".modal:not(.hidden)").forEach((modal) => closeModal(modal.id));
});

(async function init() {
  try {
    const result = await api("/api/me");
    showApp(result.user);
    await refresh();
  } catch {
    showLogin();
  }
})();
