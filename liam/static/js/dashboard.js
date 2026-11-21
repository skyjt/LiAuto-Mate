// 看板的本地 JSON、图表和空状态共用同一套显示逻辑。
const dashboard = (() => {
  const pending = new WeakMap();
  const resize = new ResizeObserver(entries => {
    for (const {target} of entries) window.echarts?.getInstanceByDom(target)?.resize();
  });

  async function json(url) {
    let response;
    try {
      response = await fetch(url);
    } catch {
      throw new Error('网络连接中断，请检查连接后重试。');
    }
    if (response.status === 401) throw new Error('看板登录已过期，请刷新页面重新登录。');
    if (!response.ok) throw new Error('读取失败，请刷新页面重试。');
    return response.json();
  }

  function message(el, text) {
    if (window.echarts) echarts.getInstanceByDom(el)?.dispose();
    resize.unobserve(el);
    const note = document.createElement('p');
    note.className = 'panel-message';
    note.setAttribute('role', 'status');
    note.textContent = text;
    el.replaceChildren(note);
  }

  async function load(el, url, render) {
    const request = Symbol();
    pending.set(el, request);
    message(el, '正在读取…');
    try {
      const data = await json(url);
      if (pending.get(el) !== request) return;
      el.replaceChildren();
      render(data, el);
    } catch (error) {
      if (pending.get(el) === request) message(el, error.message || '读取失败，请刷新重试。');
    }
  }

  function chart(el, options) {
    const instance = echarts.getInstanceByDom(el) || echarts.init(el, 'dark', {locale:'ZH'});
    instance.setOption({
      backgroundColor: 'transparent',
      aria: {enabled:true},
      animation: !matchMedia('(prefers-reduced-motion: reduce)').matches,
      grid: {left: 52, right: 36, top: 40, bottom: 32},
      tooltip: {trigger: 'axis'},
      ...options
    }, true);
    resize.observe(el);
  }

  function table(el, headings, rows) {
    const table = document.createElement('table');
    const header = table.createTHead().insertRow();
    for (const heading of headings) {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = heading;
      header.append(th);
    }
    const body = table.createTBody();
    for (const values of rows) {
      const row = body.insertRow();
      for (const value of values) row.insertCell().append(value ?? '—');
    }
    const wrap = document.createElement('div');
    wrap.className = 'table-scroll';
    wrap.append(table);
    el.append(wrap);
  }
  return {json, message, load, chart, table};
})();
