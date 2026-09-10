/* Career Radar arayüzü — derleme adımı yok, düz JavaScript.
   Tasarım turunda burası baştan yazılabilir; API sözleşmesi (schemas.py) sabit kalır. */

const state = {
  q: "", city: "", source: "", sector: "", category: [], status: "",
  flags: {},           // has_domain, has_email, ...
  sort: "name",
  page: 1,
  pageSize: 50,
  total: 0,
  selectedId: null,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

const STATUS_LABELS = {
  listemde: "listemde",
  yazdim: "yazdım",
  cevap_geldi: "cevap geldi",
  ilgilenmiyorum: "ilgilenmiyorum",
};

// taxonomy.yaml'daki 'domain' alanları — her biri style.css'de kendi rengine sahip (.cat-*).
const CATEGORY_TAXONOMY = new Set([
  "ai_ml", "veri_muhendisligi", "web_saas", "gomulu", "siber_guvenlik", "fintech",
  "savunma_havacilik", "robotik_otonom", "oyun", "bulut_devops", "saglik_bio",
  "eticaret", "danismanlik", "quantum_derin_tek",
]);
const categoryClass = (cat) => (CATEGORY_TAXONOMY.has(cat) ? `cat-${cat}` : "");

/* ------------------------------------------------------------------ sorgu */

function queryParams(extra = {}) {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.city) params.set("city", state.city);
  if (state.source) params.set("source", state.source);
  if (state.sector) params.set("sector", state.sector);
  for (const value of state.category) params.append("category", value);
  if (state.status) params.set("status", state.status);
  for (const [flag, on] of Object.entries(state.flags)) {
    if (on) params.set(flag, "true");
  }
  params.set("sort", state.sort);
  for (const [key, value] of Object.entries(extra)) params.set(key, value);
  return params;
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

/* ---------------------------------------------------------------- yükleme */

async function loadStats() {
  const s = await getJSON("/api/stats");
  const pct = (n) => (s.companies ? Math.round((n * 100) / s.companies) : 0);
  const chip = (value, label) => `<span class="stat-chip"><b>${value}</b> ${label}</span>`;
  $("stats").innerHTML = [
    chip(s.companies, "şirket"),
    chip(`${pct(s.with_domain)}%`, "alan adı"),
    chip(`${pct(s.with_careers_url)}%`, "kariyer sayfası"),
    chip(`${pct(s.with_email)}%`, "e-posta"),
    chip(s.jobs_active, "aktif ilan"),
  ].join("");
}

async function loadFacets() {
  const facets = await getJSON("/api/facets");
  const fill = (id, values, withCount = true) => {
    const select = $(id);
    const current = select.value;
    select.length = 1;
    for (const item of values) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = withCount ? `${item.label} (${item.count})` : item.label;
      select.appendChild(option);
    }
    select.value = current;
  };
  fill("city", facets.cities);
  fill("source", facets.sources);
  fill("sector", facets.sectors);
  fillCategoryChecks(facets.categories);
}

function fillCategoryChecks(values) {
  const fieldset = $("categoryChecks");
  const legend = fieldset.querySelector("legend");
  fieldset.innerHTML = "";
  fieldset.appendChild(legend);
  for (const item of values) {
    const label = document.createElement("label");
    const checked = state.category.includes(item.value) ? "checked" : "";
    label.innerHTML =
      `<input type="checkbox" data-category="${escapeHtml(item.value)}" ${checked}> ` +
      `<span class="cat-dot ${categoryClass(item.value)}"></span>` +
      `${escapeHtml(item.label)} (${item.count})`;
    fieldset.appendChild(label);
  }
  for (const box of fieldset.querySelectorAll("[data-category]")) {
    box.addEventListener("change", () => {
      const value = box.dataset.category;
      if (box.checked) {
        if (!state.category.includes(value)) state.category.push(value);
      } else {
        state.category = state.category.filter((v) => v !== value);
      }
      reload();
    });
  }
}

async function loadCompanies() {
  const params = queryParams({ page: state.page, page_size: state.pageSize });
  const data = await getJSON(`/api/companies?${params}`);
  state.total = data.total;
  renderRows(data.items);

  const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
  $("count").textContent = data.total
    ? `${data.total} şirket bulundu`
    : "Bu filtrelerle şirket yok";
  $("pageinfo").textContent = `${data.page} / ${pages}`;
  $("prev").disabled = data.page <= 1;
  $("next").disabled = data.page >= pages;
  $("csv").href = `/api/export.csv?${queryParams()}`;
}

/* ----------------------------------------------------------------- render */

function renderRows(items) {
  const tbody = $("rows");
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">Sonuç yok — filtreleri gevşetmeyi dene.</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((item) => {
    const badges = [];
    if (item.hr_email_count) badges.push('<span class="tag tag-ok">İK e-postası</span>');
    else if (item.email_count) badges.push('<span class="tag">e-posta</span>');
    if (item.has_internship) badges.push('<span class="tag tag-ok">staj ilanı</span>');
    if (!item.domain) badges.push('<span class="tag tag-warn">alan adı yok</span>');

    // categories = kendi taksonomimiz (ai_ml, web_saas...) — her biri kendi rengiyle;
    // henüz sınıflandırılmamışsa dizinin ham sektör metnine (sectors) düş (nötr renk).
    const categoryTags = item.categories.length
      ? item.categories.slice(0, 3)
          .map((s) => `<span class="tag ${categoryClass(s)}">${escapeHtml(s)}</span>`).join("")
      : item.sectors.slice(0, 2)
          .map((s) => `<span class="tag">${escapeHtml(s)}</span>`).join("");

    return `<tr data-id="${item.id}" class="${item.id === state.selectedId ? "selected" : ""}">
      <td>
        <div class="name">${escapeHtml(item.name)}</div>
        <div class="sub">${escapeHtml(item.domain || "")} ${badges.join(" ")}</div>
      </td>
      <td class="muted">${escapeHtml(item.city || "—")}</td>
      <td>${categoryTags || '<span class="muted">—</span>'}</td>
      <td class="sub">${escapeHtml(item.sources.join(", ") || "—")}</td>
      <td class="num">${item.open_jobs_count || '<span class="muted">—</span>'}</td>
      <td class="sub">${escapeHtml(STATUS_LABELS[item.user_status] || "")}</td>
    </tr>`;
  }).join("");

  for (const row of tbody.querySelectorAll("tr[data-id]")) {
    row.addEventListener("click", () => selectCompany(Number(row.dataset.id)));
  }
}

async function selectCompany(id) {
  state.selectedId = id;
  const company = await getJSON(`/api/companies/${id}`);
  const panel = $("detail");
  panel.hidden = false;

  const link = (url, text) =>
    url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(text || url)}</a>` : "";

  const contacts = company.contacts.length
    ? `<ul>${company.contacts.map((c) => `<li>
         ${c.email ? `<a href="mailto:${escapeHtml(c.email)}">${escapeHtml(c.email)}</a>` : ""}
         ${c.name ? `<div class="sub">${escapeHtml(c.name)}${c.role ? " — " + escapeHtml(c.role) : ""}</div>` : ""}
         <div class="sub">${escapeHtml(c.contact_type)} · ${link(c.source_url, "kaynak")}</div>
       </li>`).join("")}</ul>`
    : `<p class="muted">Henüz iletişim bilgisi toplanmadı.</p>`;

  const jobs = company.jobs.length
    ? `<ul>${company.jobs.map((j) => `<li>
         ${link(j.url, j.title) || escapeHtml(j.title)}
         ${j.is_internship ? '<span class="tag tag-ok">staj</span>' : ""}
         <div class="sub">${escapeHtml(j.location || "")}</div>
       </li>`).join("")}</ul>`
    : `<p class="muted">Aktif ilan bilgisi yok — doğrudan yazman gerekebilir.</p>`;

  panel.innerHTML = `
    <button class="btn btn-ghost icon-btn close" type="button" id="closeDetail" aria-label="Detayı kapat">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
    </button>
    <h2>${escapeHtml(company.name)}</h2>
    <div class="sub">${escapeHtml(company.city || "")} ${company.domain ? "· " + escapeHtml(company.domain) : ""}</div>

    <h3>Bağlantılar</h3>
    <div>${link(company.website, "web sitesi") || '<span class="muted">site bilinmiyor</span>'}</div>
    <div>${link(company.careers_url, "kariyer sayfası") || '<span class="muted">kariyer sayfası bulunamadı</span>'}</div>

    <h3>Kategori</h3>
    <div>${company.categories.map((s) => `<span class="tag ${categoryClass(s)}">${escapeHtml(s)}</span>`).join("") || '<span class="muted">henüz sınıflandırılmadı</span>'}</div>
    ${company.size_bucket || company.sector ? `<div class="sub">${escapeHtml(company.size_bucket || "")} ${escapeHtml(company.sector || "")}</div>` : ""}

    <h3>Sektör (dizin, ham)</h3>
    <div>${company.sectors.map((s) => `<span class="tag">${escapeHtml(s)}</span>`).join("") || '<span class="muted">—</span>'}</div>

    <h3>Nereden bulundu</h3>
    <div>${company.source_refs.map((s) => `<span class="tag">${escapeHtml(s.name)}</span>`).join("")}</div>

    <h3>İletişim</h3>
    ${contacts}

    <h3>Açık ilanlar</h3>
    ${jobs}

    <h3>Takibim</h3>
    <label class="field">
      <select id="userStatus">
        <option value="">—</option>
        ${Object.entries(STATUS_LABELS).map(([value, label]) =>
          `<option value="${value}" ${company.user_status === value ? "selected" : ""}>${label}</option>`).join("")}
      </select>
    </label>
    <label class="field">
      <textarea id="userNote" rows="3" placeholder="not…">${escapeHtml(company.user_note || "")}</textarea>
    </label>
    <button class="btn" type="button" id="saveNote">Kaydet</button>
  `;

  $("closeDetail").addEventListener("click", () => {
    panel.hidden = true;
    state.selectedId = null;
    loadCompanies();
  });
  $("saveNote").addEventListener("click", async () => {
    await fetch(`/api/companies/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_status: $("userStatus").value,
        user_note: $("userNote").value,
      }),
    });
    loadCompanies();
  });

  for (const row of document.querySelectorAll("#rows tr")) {
    row.classList.toggle("selected", Number(row.dataset.id) === id);
  }
}

/* ------------------------------------------------------------------ olaylar */

function reload() {
  state.page = 1;
  loadCompanies();
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* --------------------------------------------------------------------- tema */

const THEME_KEY = "theme";

function storedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch { return null; }
}

function effectiveTheme() {
  const saved = storedTheme();
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const isDark = theme === "dark";
  const btn = $("themeToggle");
  btn.querySelector(".icon-sun").hidden = isDark;
  btn.querySelector(".icon-moon").hidden = !isDark;
  btn.setAttribute("aria-label", isDark ? "Açık temaya geç" : "Koyu temaya geç");
}

function wireTheme() {
  applyTheme(effectiveTheme());
  $("themeToggle").addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    applyTheme(next);
  });
}

function wire() {
  $("q").addEventListener("input", debounce((event) => {
    state.q = event.target.value.trim();
    reload();
  }, 250));

  for (const id of ["city", "source", "sector", "status", "sort"]) {
    $(id).addEventListener("change", (event) => {
      state[id] = event.target.value;
      reload();
    });
  }

  for (const box of document.querySelectorAll("[data-flag]")) {
    box.addEventListener("change", () => {
      state.flags[box.dataset.flag] = box.checked;
      reload();
    });
  }

  $("prev").addEventListener("click", () => { state.page--; loadCompanies(); });
  $("next").addEventListener("click", () => { state.page++; loadCompanies(); });

  $("reset").addEventListener("click", () => {
    Object.assign(state, { q: "", city: "", source: "", sector: "", category: [], status: "", flags: {}, sort: "name" });
    $("q").value = "";
    for (const id of ["city", "source", "sector", "status"]) $(id).value = "";
    $("sort").value = "name";
    for (const box of document.querySelectorAll("[data-flag]")) box.checked = false;
    for (const box of document.querySelectorAll("[data-category]")) box.checked = false;
    reload();
  });
}

wire();
wireTheme();
Promise.all([loadStats(), loadFacets()])
  .then(loadCompanies)
  .catch((error) => {
    $("count").textContent = `Yüklenemedi: ${error.message}`;
  });
