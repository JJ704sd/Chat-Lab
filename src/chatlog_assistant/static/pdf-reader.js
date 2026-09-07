/* Local PDF page viewer. Viewing never submits or changes review values. */
(() => {
  const cache = new Map();
  const states = new Map();
  function mount(root, fetchPage) {
    if (root.dataset.mounted) return;
    root.dataset.mounted = 'true';
    const sha = root.dataset.pdfSha;
    const evidencePage = Number(root.dataset.pdfPage || 1);
    const bbox = JSON.parse(root.dataset.bbox || 'null');
    const key = `${sha}:${evidencePage}:${JSON.stringify(bbox)}`;
    const state = states.get(key) || {page: evidencePage, zoom: 1};
    states.set(key, state);
    let count = 0, ticket = 0, pageData, expanded = null, focusBox = bbox;
    root.classList.add('pdf-reader');
    root.innerHTML = `<div class="pdf-reader-toolbar" aria-label="PDF 阅读工具">
      <div class="pdf-reader-group"><button type="button" data-reader="prev" aria-label="PDF 上一页">‹</button>
      <select aria-label="PDF 页码" data-reader-page><option>加载页码…</option></select>
      <button type="button" data-reader="next" aria-label="PDF 下一页">›</button></div>
      <div class="pdf-reader-group"><button type="button" data-reader="out" aria-label="缩小 PDF">−</button>
      <output aria-label="PDF 缩放比例">100%</output><button type="button" data-reader="in" aria-label="放大 PDF">＋</button>
      <button type="button" data-reader="fit">适合宽度</button></div>
      <div class="pdf-reader-group"><button type="button" data-reader="locate" ${bbox ? '' : 'hidden'}>定位当前报价</button>
      <button type="button" data-reader="expand">大窗阅读</button></div></div>
      <div class="pdf-reader-status" role="status"></div>
      <div class="pdf-reader-scroll" tabindex="0" role="region" aria-label="PDF 原件，可横向和纵向滚动"><div class="pdf-reader-page"></div></div>`;
    const scroll = root.querySelector('.pdf-reader-scroll');
    const page = root.querySelector('.pdf-reader-page');
    const status = root.querySelector('.pdf-reader-status');
    const selector = root.querySelector('[data-reader-page]');
    const button = name => root.querySelector(`[data-reader="${name}"]`);
    function controls() {
      button('prev').disabled = !count || state.page <= 1;
      button('next').disabled = !count || state.page >= count;
      button('out').disabled = state.zoom <= 1;
      button('in').disabled = state.zoom >= 3;
      root.querySelector('output').textContent = `${Math.round(state.zoom * 100)}%`;
      selector.value = String(state.page);
    }
    function layout(locate = false) {
      page.style.width = `${state.zoom * 100}%`;
      controls();
      if (locate && pageData) requestAnimationFrame(() => {
        if (bbox && state.page === evidencePage) {
          const scale = page.clientWidth / pageData.width;
          scroll.scrollTop = Math.max(0, focusBox[1] * scale - 40);
          scroll.scrollLeft = Math.max(0, focusBox[0] * scale - 20);
        } else { scroll.scrollTop = 0; scroll.scrollLeft = 0; }
      });
    }
    async function load(locate = false) {
      const request = ++ticket, requestedPage = state.page;
      page.replaceChildren(); pageData = null;
      status.textContent = `正在加载第 ${requestedPage} 页…`;
      const cacheKey = `${sha}:${requestedPage}`;
      controls();
      try {
        if (!cache.has(cacheKey)) {
          const promise = fetchPage(sha, requestedPage).catch(error => {cache.delete(cacheKey); throw error;});
          cache.set(cacheKey, promise);
        }
        const result = await cache.get(cacheKey);
        if (request !== ticket || !root.isConnected) return;
        pageData = result; count = result.page_count;
        selector.replaceChildren(...Array.from({length: count}, (_, i) => new Option(`第 ${i + 1} / ${count} 页`, i + 1)));
        const image = new Image();
        image.alt = `供应商 PDF 第 ${requestedPage} 页原件`;
        image.src = result.image;
        page.style.aspectRatio = `${result.width} / ${result.height}`;
        page.append(image);
        if (bbox && requestedPage === evidencePage) {
          focusBox = result.row_regions?.find(region => region.bbox.every((v, i) => Math.abs(v - bbox[i]) < 0.1))?.focus_bbox || bbox;
          const highlight = document.createElement('div');
          highlight.className = 'pdf-reader-highlight';
          Object.assign(highlight.style, {left: `${focusBox[0] / result.width * 100}%`, top: `${focusBox[1] / result.height * 100}%`,
            width: `${(focusBox[2] - focusBox[0]) / result.width * 100}%`, height: `${(focusBox[3] - focusBox[1]) / result.height * 100}%`});
          page.append(highlight);
        }
        status.textContent = bbox ? requestedPage === evidencePage ? '橙色框为当前报价原文 · 可放大后滚动核对' :
          `正在查看第 ${requestedPage} 页；当前报价在第 ${evidencePage} 页` : `第 ${requestedPage} / ${count} 页 · 原件按比例显示`;
        layout(locate);
      } catch (error) {
        if (request !== ticket || !root.isConnected) return;
        status.textContent = error.message || 'PDF 加载失败';
        const retry = document.createElement('button');
        retry.type = 'button'; retry.textContent = '重新加载 PDF';
        retry.addEventListener('click', event => {event.stopPropagation(); load(locate);});
        page.append(retry);
      }
    }
    root.addEventListener('change', event => {
      event.stopPropagation();
      if (event.target === selector) {state.page = Number(selector.value); load(true);}
    });
    root.addEventListener('click', event => {
      event.stopPropagation();
      const action = event.target.closest('[data-reader]')?.dataset.reader;
      if (!action) return;
      if (action === 'prev' || action === 'next') {
        state.page = Math.max(1, Math.min(count, state.page + (action === 'next' ? 1 : -1))); load(true);
      } else if (action === 'locate') {state.page = evidencePage; load(true);}
      else if (action === 'expand') {
        if (expanded) {expanded.close(); return;}
        const placeholder = document.createComment('PDF reader position');
        root.before(placeholder);
        expanded = document.createElement('dialog');
        expanded.className = 'pdf-reader-dialog';
        expanded.setAttribute('aria-label', 'PDF 大窗阅读');
        document.body.append(expanded); expanded.append(root);
        button('expand').textContent = '返回对照';
        expanded.addEventListener('close', () => {
          const dialog = expanded; expanded = null;
          placeholder.replaceWith(root); dialog.remove();
          button('expand').textContent = '大窗阅读'; layout(true); button('expand').focus();
        }, {once: true});
        expanded.showModal(); layout(true);
      } else {
        const previous = state.zoom;
        state.zoom = action === 'fit' ? 1 : Math.max(1, Math.min(3, state.zoom + (action === 'in' ? 0.25 : -0.25)));
        const ratio = state.zoom / previous;
        const x = (scroll.scrollLeft + scroll.clientWidth / 2) * ratio - scroll.clientWidth / 2;
        const y = (scroll.scrollTop + scroll.clientHeight / 2) * ratio - scroll.clientHeight / 2;
        layout(); scroll.scrollLeft = Math.max(0, x); scroll.scrollTop = Math.max(0, y);
      }
    });
    load(true);
  }
  window.PdfReader = {mount};
})();
