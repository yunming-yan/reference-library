/* Reading layout and local preferences; report content is never rewritten here. */
(() => {
  'use strict';
  const root = document.documentElement;
  const byId = id => document.getElementById(id);
  const controls = ['toggleLibrary', 'toggleToc', 'focusButton', 'headerNav', 'readingSize', 'readingWidth', 'readingLineHeight'];
  const defaults = {library: true, toc: true, fontSize: 18, width: 'balanced', lineHeight: 1.8, focus: false, focusRestore: {library: true, toc: true}};
  const copy = value => JSON.parse(JSON.stringify(value));
  const widths = {narrow: '740px', balanced: '840px', wide: '1040px'};
  let reading = copy(defaults), committed = copy(defaults), initialized = false;
  let drawer = null, drawerOpener = null, inertState = [], panelState = null;
  let saveChain = Promise.resolve(), saveVersion = 0, layoutVersion = 0, pendingBookmark = null;
  let desktopMenus = innerWidth > 700;
  const isReader = () => !!current && !byId('readerView').hidden;
  const isSource = () => isReader() && currentMode === 'source';
  const isDrawer = kind => innerWidth <= (kind === 'library' ? 980 : 1240);
  const panel = kind => byId(kind === 'library' ? 'libraryPanel' : 'tocPanel');
  const normalize = value => {
    const result = copy(defaults);
    if (!value || typeof value !== 'object') return result;
    for (const name of ['library', 'toc', 'focus']) if (typeof value[name] === 'boolean') result[name] = value[name];
    if ([16, 17, 18, 20, 22].includes(value.fontSize)) result.fontSize = value.fontSize;
    if ([1.6, 1.8, 2].includes(Number(value.lineHeight))) result.lineHeight = Number(value.lineHeight);
    if (Object.hasOwn(widths, value.width)) result.width = value.width;
    if (value.focusRestore && typeof value.focusRestore === 'object') {
      for (const name of ['library', 'toc']) if (typeof value.focusRestore[name] === 'boolean') result.focusRestore[name] = value.focusRestore[name];
    }
    return result;
  };
  const toolbarBottom = () => {
    const header = document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0;
    const tools = isReader() ? document.querySelector('.reader-toolbar')?.getBoundingClientRect() : null;
    return Math.max(0, header, tools && tools.top <= header + 2 ? tools.bottom : header);
  };
  function captureBookmark() {
    if (!isReader()) return null;
    const container = byId(isSource() ? 'sourceCode' : 'content');
    const paper = document.querySelector('.paper');
    if (!container || !paper) return null;
    const ceiling = toolbarBottom(), bounds = paper.getBoundingClientRect();
    const padding = parseFloat(getComputedStyle(paper).paddingLeft) || 0;
    const x = Math.min(innerWidth - 16, Math.max(16, bounds.left + padding + 28));
    // A text caret preserves the same words when font size or line wrapping changes.
    for (const y of [ceiling + 18, ceiling + 34, ceiling + 58]) {
      if (y >= innerHeight - 12 || x < bounds.left || x > bounds.right) continue;
      let range = null;
      try {
        if (document.caretPositionFromPoint) {
          const caret = document.caretPositionFromPoint(x, y);
          if (caret) {range = document.createRange(); range.setStart(caret.offsetNode, caret.offset); range.collapse(true);}
        } else if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(x, y);
        if (range && container.contains(range.startContainer)) {
          const rect = range.getBoundingClientRect();
          if (rect.height) return {range: range.cloneRange(), offset: rect.top - ceiling, report: current.id};
        }
      } catch { /* A detached text node falls through to the block bookmark. */ }
    }
    const blocks = isSource() ? [container] : [...container.querySelectorAll('h1,h2,h3,p,li,tr,figure,blockquote,pre')];
    const target = blocks.find(node => {
      const rect = node.getBoundingClientRect();
      return rect.bottom > ceiling + 8 && rect.top < innerHeight;
    }) || paper;
    return {node: target, offset: target.getBoundingClientRect().top - ceiling, report: current.id};
  }
  function restoreBookmark(bookmark) {
    if (!bookmark || !isReader() || current.id !== bookmark.report) return;
    let rect;
    try {
      if (bookmark.range && bookmark.range.startContainer.isConnected) rect = bookmark.range.getBoundingClientRect();
      else if (bookmark.node?.isConnected) rect = bookmark.node.getBoundingClientRect();
    } catch { return; }
    if (!rect) return;
    const adjustment = rect.top - toolbarBottom() - bookmark.offset;
    if (Math.abs(adjustment) > 0.5) window.scrollBy({top: adjustment, behavior: 'auto'});
    updateProgress();
  }
  function preservePosition(change) {
    if (!pendingBookmark) pendingBookmark = captureBookmark();
    const version = ++layoutVersion;
    change();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (version !== layoutVersion) return;
      const bookmark = pendingBookmark; pendingBookmark = null;
      restoreBookmark(bookmark);
    }));
  }
  function clearBookmark() {layoutVersion++; pendingBookmark = null;}
  const panelAnimations = new Map();
  function animatePanel(kind, opening) {
    const prior = panelAnimations.get(kind);
    if (prior) {prior.animation?.cancel(); prior.ghost?.remove(); panelAnimations.delete(kind);}
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const node = panel(kind);
    if (opening) {
      requestAnimationFrame(() => {
        if (!visible(kind)) return;
        const animation = node.animate([{opacity:.4, transform:`translateX(${kind === 'library' ? -6 : 6}px)`},{opacity:1,transform:'none'}], {duration:160,easing:'cubic-bezier(.2,.7,.25,1)'});
        panelAnimations.set(kind,{animation});
      });
      return;
    }
    const rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // A noninteractive exit snapshot prevents animation from delaying layout or input.
    const ghost = node.cloneNode(true);
    ghost.removeAttribute('id');ghost.removeAttribute('role');ghost.removeAttribute('aria-modal');
    ghost.querySelectorAll('[id]').forEach(e=>e.removeAttribute('id'));
    ghost.inert=true;ghost.setAttribute('aria-hidden','true');
    Object.assign(ghost.style,{position:'fixed',left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px',display:kind==='library'?'flex':'block',pointerEvents:'none',zIndex:'44',margin:'0',maxHeight:'none'});
    document.body.append(ghost);
    const animation = ghost.animate([{opacity:.8,transform:'none'},{opacity:0,transform:`translateX(${kind === 'library' ? -12 : 12}px)`}],{duration:140,easing:'cubic-bezier(.2,.7,.25,1)'});
    panelAnimations.set(kind,{animation,ghost});
    animation.finished.catch(()=>{}).finally(()=>{ghost.remove();if(panelAnimations.get(kind)?.animation===animation)panelAnimations.delete(kind)});
  }
  function visible(kind) {
    if (isDrawer(kind)) return drawer === kind;
    return root.dataset[kind] === 'open';
  }
  function updateControl(id, on, expanded, title) {
    const button = byId(id);
    if (!button) return;
    button.setAttribute('aria-pressed', String(on));
    if (expanded !== null) button.setAttribute('aria-expanded', String(expanded));
    button.title = title; button.setAttribute('aria-label', title);
  }
  function applyReading() {
    if (innerWidth > 700 && byId('readerMore')) byId('readerMore').open = true;
    const active = isReader(), source = isSource(), focus = active && reading.focus;
    root.dataset.library = reading.library && !focus ? 'open' : 'closed';
    root.dataset.toc = active && !source && reading.toc && !focus ? 'open' : 'closed';
    root.dataset.focus = String(focus);
    root.dataset.source = String(source);
    root.style.setProperty('--reading-size', reading.fontSize + 'px');
    root.style.setProperty('--reading-line-height', String(reading.lineHeight));
    root.style.setProperty('--paper-width', widths[reading.width]);
    updateControl('toggleLibrary', visible('library'), visible('library'), visible('library') ? '收起资料导航' : '展开资料导航');
    updateControl('headerNav', visible('library'), visible('library'), visible('library') ? '收起资料导航' : '展开资料导航');
    updateControl('toggleToc', visible('toc'), visible('toc'), source ? 'MD 原文不显示章节目录' : visible('toc') ? '收起本期目录' : '展开本期目录');
    updateControl('focusButton', focus, null, focus ? '退出专注阅读，恢复导航与目录' : '进入专注阅读');
    for (const name of controls) {
      const control = byId(name);
      if (!control) continue;
      control.disabled = !initialized || (name === 'toggleToc' && (!active || source || !allHeadings.length)) || (name === 'focusButton' && !active);
    }
    if (byId('readingSize')) byId('readingSize').value = String(reading.fontSize);
    if (byId('readingWidth')) byId('readingWidth').value = reading.width;
    if (byId('readingLineHeight')) byId('readingLineHeight').value = String(reading.lineHeight);
    if (byId('readingSettings')) byId('readingSettings').setAttribute('aria-busy', String(!initialized));
  }
  function persist() {
    if (serviceCapabilities.preferences !== true) return Promise.resolve();
    const snapshot = copy(reading), version = ++saveVersion;
    saveChain = saveChain.then(async () => {
      try {
        await post('/api/settings', JSON.stringify({reading: snapshot}), {'Content-Type': 'application/json'});
        committed = copy(snapshot);
      } catch (error) {
        if (version === saveVersion) preservePosition(() => {reading = copy(committed); applyReading();});
        toast('阅读设置未能保存：' + error.message, 8000);
      }
    });
    return saveChain;
  }
  function leaveFocus() {
    if (!reading.focus) return;
    reading.library = reading.focusRestore.library;
    reading.toc = reading.focusRestore.toc;
    reading.focus = false;
  }
  function togglePanel(kind, trigger) {
    if (!initialized || (kind === 'toc' && (!isReader() || isSource() || !allHeadings.length))) return;
    if (isDrawer(kind)) {
      if (drawer === kind) closeDrawer(true);
      else openDrawer(kind, trigger);
      return;
    }
    const wasVisible = visible(kind);
    animatePanel(kind, !wasVisible);
    preservePosition(() => {leaveFocus(); reading[kind] = !wasVisible; applyReading();});
    persist();
  }
  function toggleFocus() {
    if (!initialized || !isReader()) return;
    closeDrawer(false);
    if (!reading.focus) {if (visible('library')) animatePanel('library', false); if (visible('toc')) animatePanel('toc', false);}
    preservePosition(() => {
      if (reading.focus) leaveFocus();
      else {reading.focusRestore = {library: reading.library, toc: reading.toc}; reading.focus = true;}
      applyReading();
    });
    persist();
  }
  function setBackgroundInert(activePanel) {
    inertState = [];
    let child = activePanel;
    while (child?.parentElement && child.parentElement !== document.documentElement) {
      const parent = child.parentElement;
      for (const sibling of parent.children) {
        if (sibling === child || sibling.id === 'drawerBackdrop' || ['SCRIPT', 'STYLE', 'LINK', 'DIALOG'].includes(sibling.tagName) || sibling.id === 'toast') continue;
        inertState.push([sibling, sibling.inert]); sibling.inert = true;
      }
      if (parent === document.body) break;
      child = parent;
    }
  }
  function restoreBackground() {
    for (const [node, value] of inertState) if (node.isConnected) node.inert = value;
    inertState = [];
  }
  function focusable(activePanel) {
    return [...activePanel.querySelectorAll('a[href],button,input,select,textarea,summary,[tabindex]')]
      .filter(node => !node.disabled && !node.inert && node.tabIndex >= 0 && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden');
  }
  function openDrawer(kind, trigger, search = false) {
    const activePanel = panel(kind);
    if (!activePanel || !isDrawer(kind)) return;
    closeDrawer(false);
    drawer = kind; drawerOpener = trigger || document.activeElement;
    activePanel.classList.add('open');
    document.body.dataset.drawer = kind;
    if (byId('drawerBackdrop')) byId('drawerBackdrop').hidden = false;
    panelState = {node: activePanel, role: activePanel.getAttribute('role'), modal: activePanel.getAttribute('aria-modal'), tabindex: activePanel.getAttribute('tabindex')};
    activePanel.setAttribute('role', 'dialog'); activePanel.setAttribute('aria-modal', 'true'); activePanel.tabIndex = -1;
    setBackgroundInert(activePanel); applyReading();
    requestAnimationFrame(() => {
      if (drawer !== kind) return;
      const target = search ? byId('search') : byId(kind === 'library' ? 'closeNav' : 'closeToc') || focusable(activePanel)[0] || activePanel;
      target.focus({preventScroll: true});
    });
  }
  function closeDrawer(returnFocus = true) {
    const previous = drawer, opener = drawerOpener;
    if (!previous) return;
    animatePanel(previous, false);
    drawer = null; drawerOpener = null;
    panel('library')?.classList.remove('open'); panel('toc')?.classList.remove('open');
    delete document.body.dataset.drawer;
    if (byId('drawerBackdrop')) byId('drawerBackdrop').hidden = true;
    restoreBackground();
    if (panelState) {
      for (const [attribute, name] of [['role', 'role'], ['aria-modal', 'modal'], ['tabindex', 'tabindex']]) {
        const value = panelState[name];
        if (value === null) panelState.node.removeAttribute(attribute); else panelState.node.setAttribute(attribute, value);
      }
      panelState = null;
    }
    applyReading();
    if (returnFocus && opener?.isConnected && !opener.inert && opener.getClientRects().length) opener.focus({preventScroll: true});
  }
  function closePanel(kind) {
    if (drawer === kind) {closeDrawer(true); return;}
    if (!isDrawer(kind)) {
      animatePanel(kind, false);
      preservePosition(() => {leaveFocus(); reading[kind] = false; applyReading();});
      persist();
    }
  }
  for (const id of ['toggleLibrary', 'headerNav', 'mobileNav', 'homeNav']) {
    const button = byId(id);
    if (button) button.onclick = () => togglePanel('library', button);
  }
  if (byId('toggleToc')) byId('toggleToc').onclick = event => togglePanel('toc', event.currentTarget);
  if (byId('focusButton')) byId('focusButton').onclick = toggleFocus;
  if (byId('closeNav')) byId('closeNav').onclick = () => closePanel('library');
  if (byId('closeToc')) byId('closeToc').onclick = () => closePanel('toc');
  if (byId('drawerBackdrop')) byId('drawerBackdrop').onclick = () => closeDrawer(true);
  if (byId('readingSize')) byId('readingSize').onchange = event => {
    const value = Number(event.target.value);
    if (![16, 17, 18, 20, 22].includes(value)) return;
    preservePosition(() => {reading.fontSize = value; applyReading();}); persist();
  };
  if (byId('readingWidth')) byId('readingWidth').onchange = event => {
    const value = event.target.value;
    if (!Object.hasOwn(widths, value)) return;
    preservePosition(() => {reading.width = value; applyReading();}); persist();
  };
  if (byId('readingLineHeight')) byId('readingLineHeight').onchange = event => {
    const value = Number(event.target.value);
    if (![1.6, 1.8, 2].includes(value)) return;
    preservePosition(() => {reading.lineHeight = value; applyReading();}); persist();
  };
  document.addEventListener('keydown', event => {
    if (document.querySelector('dialog[open]')) return;
    if (drawer) {
      if (event.key === 'Escape') {event.preventDefault(); event.stopImmediatePropagation(); closeDrawer(true); return;}
      if (event.key === 'Tab') {
        const activePanel = panel(drawer), items = focusable(activePanel);
        const first = items[0], last = items.at(-1), active = document.activeElement;
        if (!first) {event.preventDefault(); activePanel.focus();}
        else if (event.shiftKey && (active === first || !activePanel.contains(active))) {event.preventDefault(); last.focus();}
        else if (!event.shiftKey && (active === last || !activePanel.contains(active))) {event.preventDefault(); first.focus();}
      }
      return;
    }
    const editable = document.activeElement?.matches('input,textarea,select,[contenteditable="true"]');
    if (event.key === '/' && editable) {event.stopImmediatePropagation(); return;}
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && initialized) {
      event.preventDefault(); event.stopImmediatePropagation();
      if (isDrawer('library')) openDrawer('library', byId('headerNav'), true);
      else {
        if (!visible('library')) {preservePosition(() => {leaveFocus(); reading.library = true; applyReading();}); persist();}
        byId('search').focus({preventScroll: true});
      }
    }
    if (event.key === 'Escape') {
      const menu = byId('readingSettings')?.open ? byId('readingSettings') : innerWidth <= 700 && byId('readerMore')?.open ? byId('readerMore') : null;
      if (menu) {event.preventDefault(); menu.open = false; menu.querySelector('summary')?.focus({preventScroll: true});}
    }
  }, true);
  document.addEventListener('click', event => {
    const more = byId('readerMore'), settings = byId('readingSettings');
    if (innerWidth <= 700 && more && (!more.contains(event.target) || event.target.closest('#manageButton,#toBottom,#figuresButton,.export-menu a'))) more.open = false;
    if (settings && !settings.contains(event.target)) settings.open = false;
    const chapter = event.target.closest('[data-chapter]');
    if (drawer && chapter) {
      const target = byId('chapter-' + chapter.dataset.chapter);
      closeDrawer(false);
      if (target) {target.tabIndex = -1; target.focus({preventScroll: true});}
    } else if (drawer && event.target.closest('#storageButton')) closeDrawer(false);
  });
  window.addEventListener('resize', () => {
    const nextDesktopMenus = innerWidth > 700;
    if (!nextDesktopMenus && desktopMenus && byId('readerMore')) byId('readerMore').open = false;
    desktopMenus = nextDesktopMenus;
    if (drawer && !isDrawer(drawer)) closeDrawer(true);
    applyReading();
  }, {passive: true});
  // Hook the existing renderer only for control state; its report and anchor logic stays intact.
  const originalRender = renderReportView, originalOpen = openReport, originalHome = showHome, originalRenderHome = renderHome;
  renderReportView = function(...args) {clearBookmark(); closeDrawer(false); const result = originalRender.apply(this, args); applyReading(); return result;};
  openReport = async function(...args) {
    const fromDrawer = !!drawer; clearBookmark(); closeDrawer(false);
    const result = await originalOpen.apply(this, args);
    applyReading();
    if (fromDrawer && current?.id === args[0]) {const title = byId('content')?.querySelector('h1'); if (title) {title.tabIndex = -1; title.focus({preventScroll: true});}}
    return result;
  };
  showHome = function(...args) {clearBookmark(); closeDrawer(false); const result = originalHome.apply(this, args); applyReading(); return result;};
  renderHome = function(...args) {const result = originalRenderHome.apply(this, args); applyReading(); return result;};
  for (const id of ['toggleLibrary', 'headerNav', 'mobileNav', 'homeNav']) byId(id)?.setAttribute('aria-controls', 'libraryPanel');
  byId('toggleToc')?.setAttribute('aria-controls', 'tocPanel');
  applyReading();
  Promise.resolve(window.archiveStartup || api('/api/library')).then(data => {reading = normalize(data.settings?.reading); committed = copy(reading);})
    .catch(error => toast('阅读设置暂未读取：' + error.message, 8000))
    .finally(() => {initialized = true; root.dataset.readingReady = 'true'; applyReading();});
})();
