let totalPages  = 0;
let currentPage = 0;
let currentFiles = [];
let markedFiles  = new Set();

// ══════════════════════════════════════════════════════════════════════════════
//  ДОМЕНЫ
// ══════════════════════════════════════════════════════════════════════════════

async function initDomains() {
    const domains = await eel.get_blocked_domains()();
    renderDomains(domains);
}

function renderDomains(domains) {
    const list = document.getElementById('domains-list');
    list.innerHTML = '';
    domains.forEach(d => {
        const tag = document.createElement('div');
        tag.className = 'domain-tag';
        tag.innerHTML = `<span>${d}</span><button class="remove-btn" onclick="removeDomain('${d}')" title="Удалить">×</button>`;
        list.appendChild(tag);
    });
}

function toggleDomains() {
    const body = document.getElementById('domains-body');
    const icon = document.getElementById('domains-toggle-icon');
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    icon.classList.toggle('open', !isOpen);
}

async function addDomain() {
    const input  = document.getElementById('domain-input');
    const domain = input.value.trim().toLowerCase();
    if (!domain) return;
    const res = await eel.add_blocked_domain(domain)();
    if (res.ok) {
        input.value = '';
        renderDomains(res.domains);
    } else {
        input.style.borderColor = 'var(--error)';
        setTimeout(() => input.style.borderColor = '', 1200);
    }
}

async function removeDomain(domain) {
    const res = await eel.remove_blocked_domain(domain)();
    if (res.ok) renderDomains(res.domains);
}

window.addEventListener('load', initDomains);


// ══════════════════════════════════════════════════════════════════════════════
//  ЗАПУСК
// ══════════════════════════════════════════════════════════════════════════════

async function startApp() {
    const hasKeys = await eel.check_keys()();
    if (hasKeys) {
        document.getElementById('btn-start').disabled = true;
        document.getElementById('btn-apply').disabled = true;
        eel.start_parsing()();
    }
}


// ══════════════════════════════════════════════════════════════════════════════
//  EEL — ВХОДЯЩИЕ ВЫЗОВЫ ИЗ PYTHON
// ══════════════════════════════════════════════════════════════════════════════

eel.expose(add_log);
function add_log(msg, tag) {
    const box = document.getElementById('log-box');
    const p   = document.createElement('p');
    p.textContent = msg;
    if (tag) p.className = tag;
    box.appendChild(p);
    box.scrollTop = box.scrollHeight;
}

eel.expose(update_overall_progress);
function update_overall_progress(done, total) {
    const pct = total > 0 ? Math.round((done/total)*100) : 0;
    document.getElementById('prog-total-fill').style.width = pct + '%';
    document.getElementById('prog-total-text').innerText   = pct + '%';
}

eel.expose(update_folder_progress);
function update_folder_progress(done, total, label) {
    const pct = total > 0 ? Math.round((done/total)*100) : 0;
    document.getElementById('prog-folder-fill').style.width = pct + '%';
    document.getElementById('prog-folder-text').innerText =
        total > 1 ? `${label.substring(0,22)}: ${done}/${total}` : '';
}

eel.expose(update_pages_count);
function update_pages_count(count) {
    totalPages = count;
    // Показываем первую страницу сразу как только она появилась
    if (currentPage === 0 && totalPages === 1) loadPage(0);
    updateNavUI();
}

eel.expose(add_live_thumb);
function add_live_thumb(pageIndex, path) {
    if (pageIndex !== currentPage) return;
    if (!currentFiles.includes(path)) currentFiles.push(path);
    renderCard(path, false);   // false = не добавлять timestamp (свежий файл)
    updateStatsUI();
}

eel.expose(mark_as_suspicious);
function mark_as_suspicious(pageIndex, path) {
    markedFiles.add(path);
    if (pageIndex === currentPage) {
        const card = document.querySelector(`.thumb-card[data-path="${CSS.escape(path)}"]`);
        if (card) card.classList.add('selected');
        updateStatsUI();
    }
}

eel.expose(refresh_page_if_active);
function refresh_page_if_active(pageIndex, pageData) {
    if (pageIndex === currentPage) renderPageData(pageData);
}

eel.expose(parsing_complete);
function parsing_complete() {
    document.getElementById('btn-apply').disabled = false;
}

eel.expose(apply_complete);
function apply_complete() {
    markedFiles.clear();
    loadPage(currentPage);
    document.getElementById('btn-apply').disabled = false;
}


// ══════════════════════════════════════════════════════════════════════════════
//  НАВИГАЦИЯ И РЕНДЕР
// ══════════════════════════════════════════════════════════════════════════════

async function loadPage(index) {
    const data = await eel.get_page(index)();
    if (!data) return;
    currentPage = index;
    renderPageData(data);
    updateNavUI();
}

function renderPageData(data) {
    currentFiles = data.files;
    document.getElementById('page-title').innerText = `Папка ${data.folder} · ${data.query}`;
    const grid = document.getElementById('grid-container');
    grid.innerHTML = '';
    // true = добавлять timestamp (файлы могли измениться после боке)
    currentFiles.forEach(path => renderCard(path, true));
    updateStatsUI();
}

function renderCard(path, bustCache) {
    const grid = document.getElementById('grid-container');
    const card = document.createElement('div');
    card.className    = 'thumb-card';
    card.dataset.path = path;
    if (markedFiles.has(path)) card.classList.add('selected');

    const img  = document.createElement('img');
    const src  = '/img/' + encodeURI(path);
    img.src    = bustCache ? src + '?t=' + Date.now() : src;
    img.loading = 'lazy';
    card.appendChild(img);

    card.onclick = () => {
        if (markedFiles.has(path)) {
            markedFiles.delete(path);
            card.classList.remove('selected');
        } else {
            markedFiles.add(path);
            card.classList.add('selected');
        }
        updateStatsUI();
    };

    card.oncontextmenu = (e) => {
        e.preventDefault();
        openPreview(src);
    };

    grid.appendChild(card);
}

function updateStatsUI() {
    let marked = 0;
    currentFiles.forEach(p => { if (markedFiles.has(p)) marked++; });
    let text = `${currentFiles.length} фото`;
    if (marked > 0) text += ` · Отмечено: <span style="color:var(--accent)">${marked}</span>`;
    document.getElementById('page-stats').innerHTML = text;
}

function updateNavUI() {
    document.getElementById('nav-label').innerText  = `${currentPage+1} / ${totalPages}`;
    document.getElementById('btn-prev').disabled    = currentPage <= 0;
    document.getElementById('btn-next').disabled    = currentPage >= totalPages-1;
}

function changePage(delta) {
    const next = currentPage + delta;
    if (next >= 0 && next < totalPages) loadPage(next);
}

function applyChanges() {
    // Без confirm — пользователь знает что делает
    document.getElementById('btn-apply').disabled = true;
    eel.apply_actions(Array.from(markedFiles))();
}


// ══════════════════════════════════════════════════════════════════════════════
//  ПРЕВЬЮ
// ══════════════════════════════════════════════════════════════════════════════

function openPreview(src) {
    document.getElementById('preview-img').src = src;
    document.getElementById('preview-modal').style.display = 'flex';
}

function closePreview() {
    document.getElementById('preview-modal').style.display = 'none';
    document.getElementById('preview-img').src = '';
}