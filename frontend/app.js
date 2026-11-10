/* FX Wishlist browser app: reads and writes data through the local FastAPI API. */

const appRoot = document.querySelector("#app");
const currencies = [
  { code: "USD", name: "미국 달러", flag: "🇺🇸" },
  { code: "JPY", name: "일본 엔", flag: "🇯🇵" },
  { code: "CNY", name: "중국 위안", flag: "🇨🇳" },
  { code: "EUR", name: "유럽 유로", flag: "🇪🇺" },
];
let activeChart = null;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = body && typeof body === "object" ? body.detail : body;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.message || JSON.stringify(item)).join(" · ")
      : detail || `요청에 실패했어요 (${response.status})`;
    throw new ApiError(message, response.status);
  }
  return body;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function money(value, currency = "KRW") {
  if (value === null || value === undefined || value === "") return "가격 기록 없음";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "가격 기록 없음";
  const digits = currency === "KRW" ? 0 : 2;
  return new Intl.NumberFormat("ko-KR", {
    style: "currency", currency, maximumFractionDigits: digits,
    minimumFractionDigits: currency === "KRW" ? 0 : 2,
  }).format(amount);
}

function rateAmount(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "환율 없음";
  const formatted = new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: 2, maximumFractionDigits: 6,
  }).format(Number(value));
  return `₩${formatted}`;
}

function changeLabel(value) {
  if (value === null || value === undefined) return '<span class="change neutral">기록 없음</span>';
  const number = Number(value);
  const direction = number > 0 ? "up" : number < 0 ? "down" : "neutral";
  const sign = number > 0 ? "+" : "";
  return `<span class="change ${direction}">${sign}${number.toFixed(2)}%</span>`;
}

function imageUrl(path) {
  if (!path) return "";
  const filename = String(path).split("/").pop();
  return `/uploads/${encodeURIComponent(filename)}`;
}

function imageMarkup(path, className = "product-image") {
  const src = imageUrl(path);
  return src
    ? `<div class="${className}"><img src="${escapeHtml(src)}" alt="상품 이미지" loading="lazy" onerror="this.parentElement.innerHTML='♡'"></div>`
    : `<div class="${className}" aria-label="상품 이미지 없음">♡</div>`;
}

function loading(message = "화면을 준비하고 있어요…") {
  appRoot.innerHTML = `<div class="loading-state" role="status"><span class="spinner"></span>${escapeHtml(message)}</div>`;
}

function showError(error, retryAction = "renderRoute()") {
  appRoot.innerHTML = `
    <section class="error-state" role="alert">
      <span class="empty-icon" aria-hidden="true">☁️</span>
      <h3>데이터를 불러오지 못했어요</h3>
      <p>${escapeHtml(error.message || "서버 연결을 확인해 주세요.")}</p>
      <button class="button" type="button" onclick="${retryAction}">다시 시도</button>
    </section>`;
}

function routeInfo() {
  const hash = location.hash || "#/";
  if (hash === "#/" || hash === "#") return { page: "home" };
  const detail = hash.match(/^#\/product\/(\d+)$/);
  if (detail) return { page: "detail", id: Number(detail[1]) };
  if (hash === "#/add") return { page: "form", mode: "add" };
  const edit = hash.match(/^#\/edit\/(\d+)$/);
  if (edit) return { page: "form", mode: "edit", id: Number(edit[1]) };
  return { page: "not-found" };
}

async function renderRoute() {
  if (activeChart) {
    activeChart.destroy();
    activeChart = null;
  }
  const route = routeInfo();
  loading(route.page === "home" ? "위시리스트를 불러오는 중이에요…" : "페이지를 준비하고 있어요…");
  try {
    if (route.page === "home") await renderHome();
    else if (route.page === "detail") await renderDetail(route.id);
    else if (route.page === "form") await renderProductForm(route.mode, route.id);
    else appRoot.innerHTML = '<div class="empty-state"><span class="empty-icon">🔎</span><h3>페이지를 찾을 수 없어요</h3><a class="button" href="#/">홈으로</a></div>';
    appRoot.focus({ preventScroll: true });
  } catch (error) {
    showError(error);
  }
}

function renderRates(rateRows) {
  const byCurrency = new Map(rateRows.map((row) => [row.currency, row]));
  return currencies.map((currency) => {
    const rate = byCurrency.get(currency.code);
    return `
      <article class="rate-card">
        <div class="rate-card-top"><span class="rate-name">${currency.name}</span><span class="rate-flag" aria-hidden="true">${currency.flag}</span></div>
        <p class="rate-code">${currency.code} <span class="small-label">→ KRW</span></p>
        <p class="rate-value">${rate ? `${rateAmount(rate.krw_rate)} <span class="small-label">/ 1 ${currency.code}</span>` : "데이터 없음"}</p>
        <p class="rate-date">${rate ? `기준일 ${escapeHtml(rate.rate_date)}` : "환율 갱신 후 표시됩니다"}</p>
      </article>`;
  }).join("");
}

async function productCard(product) {
  let price = null;
  let changes = null;
  try { price = await api(`/products/${product.id}/price`); } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
  }
  try { changes = await api(`/products/${product.id}/price-changes`); } catch { /* Price card still shows even if history is not ready. */ }
  const localPrice = `${escapeHtml(product.currency)} ${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(Number(product.original_price))}`;
  return `
    <a class="product-card" href="#/product/${product.id}" aria-label="${escapeHtml(product.name)} 상세 보기">
      ${imageMarkup(product.image_path)}
      <div class="product-content">
        <div class="product-title-row"><h3 class="product-title">${escapeHtml(product.name)}</h3><span class="currency-pill">${escapeHtml(product.currency)}</span></div>
        <p class="local-price">${localPrice}</p>
        <p class="won-price">${price ? money(price.converted_price) : "아직 가격 기록이 없습니다"}</p>
        <div class="card-foot"><span class="small-label">등록 당시 대비</span>${changeLabel(changes?.registration_change_percent)}</div>
      </div>
    </a>`;
}

async function renderHome() {
  const [ratesResult, productsResult] = await Promise.allSettled([
    api("/exchange-rates"), api("/products"),
  ]);
  const rates = ratesResult.status === "fulfilled" ? ratesResult.value : [];
  const products = productsResult.status === "fulfilled" ? productsResult.value : [];
  appRoot.innerHTML = `
    <section class="hero">
      <div><p class="eyebrow">A little list of lovely finds</p><h1>FX Wishlist</h1><p class="hero-copy">마음에 담아둔 해외 상품과 원화 가격을 한눈에 살펴보세요.</p></div>
      <div class="hero-actions">
        <button class="button secondary" id="refresh-data" type="button">↻ 환율·가격 갱신</button>
      </div>
    </section>
    ${ratesResult.status === "rejected" ? `<div class="notice" role="alert">환율 정보를 불러오지 못했어요. ${escapeHtml(ratesResult.reason.message)}</div>` : ""}
    ${productsResult.status === "rejected" ? `<div class="notice" role="alert">상품 목록을 불러오지 못했어요. ${escapeHtml(productsResult.reason.message)}</div>` : ""}
    <section aria-labelledby="rates-title">
      <div class="section-heading"><h2 id="rates-title">오늘의 적용 환율</h2><span class="section-note">외화 1단위당 원화 · API 기준일</span></div>
      <div class="rate-grid">${renderRates(rates)}</div>
    </section>
    <section aria-labelledby="wishlist-title">
      <div class="section-heading"><h2 id="wishlist-title">내 위시리스트 <span class="small-label">${productsResult.status === "fulfilled" ? products.length : "—"}</span></h2><span class="section-note">다시 보고 싶은 것들</span></div>
      ${productsResult.status === "rejected" ? "" : products.length
        ? `<div class="wishlist-grid" id="wishlist-grid"><div class="loading-state"><span class="spinner"></span>상품 가격을 확인하고 있어요…</div></div>`
        : `<div class="empty-state"><span class="empty-icon" aria-hidden="true">🧺</span><h3>위시리스트가 아직 비어 있어요</h3><p>눈여겨본 상품을 하나 추가해 보세요. 가격이 기록되면 원화 환산 금액과 변동률도 여기에 표시됩니다.</p></div>`}
    </section>
    <a class="button add-dock" href="#/add"><span aria-hidden="true">＋</span> 상품 추가</a>`;

  appRoot.querySelector("#refresh-data")?.addEventListener("click", refreshAllData);
  const grid = appRoot.querySelector("#wishlist-grid");
  if (grid) {
    try {
      const cards = await Promise.all(products.map(productCard));
      if (grid.isConnected) grid.innerHTML = cards.join("");
    } catch (error) {
      if (grid.isConnected) grid.innerHTML = `<div class="notice" role="alert">가격 정보를 불러오지 못했어요. ${escapeHtml(error.message)}</div>`;
    }
  }
}

async function refreshAllData(event) {
  const button = event.currentTarget;
  button.disabled = true;
  const before = button.textContent;
  button.textContent = "환율을 가져오는 중…";
  try {
    await api("/exchange-rates/refresh", { method: "POST" });
    button.textContent = "가격을 기록하는 중…";
    const result = await api("/prices/refresh", { method: "POST" });
    const skipped = result.skipped?.length || 0;
    await renderHome();
    if (skipped) {
      const notice = document.createElement("div");
      notice.className = "notice";
      notice.setAttribute("role", "status");
      notice.textContent = `환율 데이터가 없는 상품 ${skipped}개는 가격 기록을 건너뛰었어요.`;
      appRoot.querySelector(".hero")?.after(notice);
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = before;
    const notice = document.createElement("div");
    notice.className = "notice";
    notice.setAttribute("role", "alert");
    notice.textContent = `데이터를 갱신하지 못했어요. ${error.message}`;
    appRoot.querySelector(".hero")?.after(notice);
  }
}

async function renderDetail(productId) {
  const product = await api(`/products/${productId}`);
  const [historyResult, changesResult] = await Promise.allSettled([
    api(`/products/${productId}/price-history`),
    api(`/products/${productId}/price-changes`),
  ]);
  const history = historyResult.status === "fulfilled" ? historyResult.value : [];
  const changes = changesResult.status === "fulfilled" ? changesResult.value : {};
  const latest = history.at(-1);
  const productUrl = product.url && /^https?:\/\//i.test(product.url) ? product.url : null;
  appRoot.innerHTML = `
    <div class="detail-top"><a class="back-link" href="#/">← 내 위시리스트로</a><span class="currency-pill">${escapeHtml(product.currency)}</span></div>
    <div class="detail-layout">
      <article class="panel detail-card">
        ${imageMarkup(product.image_path, "detail-photo")}
        <div class="detail-info">
          <p class="eyebrow">Your saved find</p>
          <h1>${escapeHtml(product.name)}</h1>
          ${productUrl ? `<a class="product-url" href="${escapeHtml(productUrl)}" target="_blank" rel="noopener noreferrer">상품 페이지 열기 ↗</a>` : '<span class="small-label">상품 URL이 등록되지 않았어요</span>'}
          <div class="detail-price-box"><p class="small-label">현재 원화 환산 가격</p><p class="won-price">${latest ? money(latest.converted_price) : "가격 기록 없음"}</p><p class="detail-local">현지 가격 ${escapeHtml(product.currency)} ${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(Number(product.original_price))}${latest ? ` · 환율 기준일 ${escapeHtml(latest.exchange_rate_date)}` : ""}</p></div>
          <div class="metrics">
            <div class="metric"><p class="metric-label">등록 당시 대비</p><p class="metric-value">${changeLabel(changes.registration_change_percent)}</p></div>
            <div class="metric"><p class="metric-label">직전 기록 대비</p><p class="metric-value">${changeLabel(changes.previous_change_percent)}</p></div>
          </div>
          <div class="detail-actions">
            <button class="button small secondary" id="refresh-one" type="button">↻ 가격 기록</button>
            <a class="button small secondary" href="#/edit/${product.id}">상품 수정</a>
            <button class="button small danger" id="delete-product" type="button">상품 삭제</button>
          </div>
          ${!history.length ? '<div class="notice">아직 가격 기록이 없습니다. 환율·가격 갱신을 눌러 첫 기록을 만들어 보세요.</div>' : ""}
        </div>
      </article>
      <section class="panel chart-panel" aria-labelledby="chart-title">
        <div class="chart-title-row"><div><p class="eyebrow">Price story</p><h2 id="chart-title">원화 가격 추이</h2></div><span class="currency-pill">KRW</span></div>
        <p class="chart-caption">그래프는 상품의 현지 가격이 아니라, 당시 환율을 적용한 원화 환산 가격을 보여줍니다.</p>
        ${history.length ? '<div class="chart-wrap"><canvas id="price-chart" role="img" aria-label="날짜별 원화 환산 가격 그래프"></canvas></div>' : '<div class="chart-empty"><span class="empty-icon">🌱</span><strong>아직 가격 기록이 없습니다</strong><span>환율·가격 갱신 후 날짜별 흐름이 여기에 나타나요.</span></div>'}
        ${history.length ? `<div class="history-list" aria-label="가격 이력">${history.map((row) => `<div class="history-row"><span>${escapeHtml(row.history_date)}</span><span>환율일 ${escapeHtml(row.exchange_rate_date)}</span><span>${money(row.converted_price)}</span></div>`).join("")}</div>` : ""}
      </section>
    </div>`;
  if (history.length) drawPriceChart(history);
  appRoot.querySelector("#refresh-one")?.addEventListener("click", () => refreshOnePrice(productId));
  appRoot.querySelector("#delete-product")?.addEventListener("click", () => deleteProduct(productId));
}

function drawPriceChart(history) {
  const canvas = appRoot.querySelector("#price-chart");
  if (!canvas || !window.Chart) {
    const wrap = canvas?.parentElement;
    if (wrap) wrap.innerHTML = '<div class="chart-empty">그래프 라이브러리를 불러오지 못했어요. 아래 가격 이력을 확인해 주세요.</div>';
    return;
  }
  activeChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: history.map((row) => row.history_date),
      datasets: [{
        label: "원화 환산 가격", data: history.map((row) => Number(row.converted_price)),
        borderColor: "#854404", backgroundColor: "rgba(251, 192, 222, .42)",
        pointBackgroundColor: "#e995bb", pointBorderColor: "#fffefa", pointBorderWidth: 2,
        pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5, fill: true, tension: .32,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (context) => ` ${money(context.parsed.y)}` } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#8e8172", maxTicksLimit: 7 } },
        y: { beginAtZero: false, grid: { color: "rgba(133,68,4,.08)" }, ticks: { color: "#8e8172", callback: (value) => money(value) } },
      },
    },
  });
}

async function refreshOnePrice(productId) {
  const button = appRoot.querySelector("#refresh-one");
  if (button) { button.disabled = true; button.textContent = "환율 적용 중…"; }
  try {
    await api(`/products/${productId}/price/refresh`, { method: "POST" });
    await renderDetail(productId);
  } catch (error) {
    if (button) { button.disabled = false; button.textContent = "↻ 가격 기록"; }
    const notice = document.createElement("div");
    notice.className = "notice";
    notice.setAttribute("role", "alert");
    notice.textContent = error.message;
    appRoot.querySelector(".detail-info")?.append(notice);
  }
}

async function deleteProduct(productId) {
  if (!window.confirm("이 상품과 저장된 가격 이력을 삭제할까요?")) return;
  try {
    await api(`/products/${productId}`, { method: "DELETE" });
    location.hash = "#/";
  } catch (error) {
    window.alert(`상품을 삭제하지 못했어요. ${error.message}`);
  }
}

async function renderProductForm(mode, productId) {
  const editing = mode === "edit";
  const product = editing ? await api(`/products/${productId}`) : null;
  appRoot.innerHTML = `
    <div class="detail-top"><a class="back-link" href="${editing ? `#/product/${productId}` : "#/"}">← ${editing ? "상품 상세로" : "위시리스트로"}</a></div>
    <section class="panel form-panel">
      <div class="form-heading"><p class="eyebrow">${editing ? "Edit your find" : "Save a little wish"}</p><h1>${editing ? "상품 정보 수정" : "상품 추가"}</h1><p>상품 정보를 입력해 주세요. 가격과 환율을 연결해 원화 가격을 기록할 수 있어요.</p></div>
      <form class="product-form" id="product-form">
        <div class="field"><label for="name">상품명 <span aria-hidden="true">*</span></label><input id="name" name="name" required maxlength="200" value="${escapeHtml(product?.name || "")}" placeholder="예: 빈티지 가죽 가방"></div>
        <div class="field"><label for="url">상품 URL <span class="optional">선택</span></label><input id="url" name="url" type="url" maxlength="2048" value="${escapeHtml(product?.url || "")}" placeholder="https://"></div>
        <div class="field"><label for="original_price">현지 가격 <span aria-hidden="true">*</span></label><input id="original_price" name="original_price" type="number" min="0.01" step="0.01" required value="${product ? escapeHtml(product.original_price) : ""}" placeholder="0.00"></div>
        <div class="field"><label for="currency">통화 <span aria-hidden="true">*</span></label><select id="currency" name="currency" required>${currencies.map((item) => `<option value="${item.code}" ${product?.currency === item.code ? "selected" : ""}>${item.flag} ${item.code} · ${item.name}</option>`).join("")}</select></div>
        <div class="field"><label for="image">상품 이미지 <span class="optional">선택 · 최대 5MB</span></label><input id="image" name="image" type="file" accept="image/jpeg,image/png,image/gif,image/webp,image/bmp"><p class="field-hint">이미지 파일을 선택하면 서버에 업로드됩니다. ${editing ? "새 파일을 선택하지 않으면 기존 이미지가 유지됩니다." : ""}</p>${product?.image_path ? `<div class="image-preview" id="image-preview">${imageMarkup(product.image_path, "")}<span>현재 등록된 이미지</span></div>` : '<div class="image-preview" id="image-preview"><span>선택한 이미지 미리보기</span></div>'}</div>
        <div id="form-error" aria-live="polite"></div>
        <div class="form-actions"><a class="button secondary" href="${editing ? `#/product/${productId}` : "#/"}">취소</a><button class="button" type="submit">${editing ? "수정 저장" : "위시리스트에 추가"}</button></div>
      </form>
    </section>`;
  const form = appRoot.querySelector("#product-form");
  const fileInput = form.elements.image;
  fileInput.addEventListener("change", () => previewSelectedImage(fileInput.files[0]));
  form.addEventListener("submit", (event) => submitProductForm(event, form, mode, productId));
}

function previewSelectedImage(file) {
  const preview = appRoot.querySelector("#image-preview");
  if (!preview || !file) return;
  if (!file.type.startsWith("image/")) {
    preview.innerHTML = "<span>이미지 파일을 선택해 주세요.</span>";
    return;
  }
  const image = document.createElement("img");
  image.alt = "선택한 상품 이미지 미리보기";
  image.src = URL.createObjectURL(file);
  preview.replaceChildren(image, Object.assign(document.createElement("span"), { textContent: file.name }));
}

async function submitProductForm(event, form, mode, productId) {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const errorBox = form.querySelector("#form-error");
  const formData = new FormData(form);
  if (!formData.get("image")?.size) formData.delete("image");
  button.disabled = true;
  errorBox.innerHTML = "";
  button.textContent = "저장 중…";
  try {
    await api(mode === "edit" ? `/products/${productId}` : "/products", {
      method: mode === "edit" ? "PUT" : "POST", body: formData,
    });
    location.hash = mode === "edit" ? `#/product/${productId}` : "#/";
  } catch (error) {
    errorBox.innerHTML = `<div class="form-error" role="alert">${escapeHtml(error.message)}</div>`;
    button.disabled = false;
    button.textContent = mode === "edit" ? "수정 저장" : "위시리스트에 추가";
  }
}

window.addEventListener("hashchange", renderRoute);
window.addEventListener("DOMContentLoaded", renderRoute);
