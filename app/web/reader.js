/* Reading layout and local preferences; report content is never rewritten here. */
(() => {
  'use strict';
  const root = document.documentElement;
  const byId = id => document.getElementById(id);
  const controls = ['toggleLibrary', 'toggleToc', 'focusButton', 'headerNav', 'readingSize', 'readingWidth', 'readingLineHeight'];
  const defaults = {library: true, toc: true, fontSize: 18, width: 'balanced', lineHeight: 1.8, focus: false, focusRestore: {library: true, toc: true}};
  const copy = value => JSON.parse(JSON.stringify(value));
  const widths = {narrow: '760px', balanced: '1100px', wide: '1240px'};
  let reading = copy(defaults), committed = copy(defaults), initialized = false;
  let drawer = null, drawerOpener = null, inertState = [], panelState = null;
  let saveChain = Promise.resolve(), saveVersion = 0, layoutVersion = 0, pendingBookmark = null;
  let desktopMenus = false;
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
    const header = root.dataset.page === 'reader' ? 0 : document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0;
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
  function clearBookmark() {layoutVersion++; pendingBookmark = null; cancelPanelAnimations();}
  const panelAnimations = new Map();
  function cancelPanelAnimation(kind) {
    const prior = panelAnimations.get(kind);
    if (!prior) return;
    if (prior.frame !== undefined) cancelAnimationFrame(prior.frame);
    prior.animation?.cancel(); prior.ghost?.remove(); panelAnimations.delete(kind);
  }
  function cancelPanelAnimations() {for (const kind of [...panelAnimations.keys()]) cancelPanelAnimation(kind);}
  const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
  motionPreference.addEventListener('change', event => {if (event.matches) cancelPanelAnimations();});
  function animatePanel(kind, opening) {
    cancelPanelAnimation(kind);
    if (motionPreference.matches) return;
    const node = panel(kind);
    if (opening) {
      const record = {};
      // Register before the frame: a reverse action cancels both pending and running motion.
      panelAnimations.set(kind, record);
      record.frame = requestAnimationFrame(() => {
        if (panelAnimations.get(kind) !== record || !visible(kind) || motionPreference.matches) {
          if (panelAnimations.get(kind) === record) panelAnimations.delete(kind);
          return;
        }
        delete record.frame;
        record.animation = node.animate([{opacity:.4, transform:'translateX('+(kind === 'library' ? -6 : 6)+'px)'},{opacity:1,transform:'none'}], {duration:160,easing:'cubic-bezier(.2,.7,.25,1)'});
        record.animation.finished.catch(()=>{}).finally(()=>{if (panelAnimations.get(kind) === record) panelAnimations.delete(kind);});
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
    const animation = ghost.animate([{opacity:.8,transform:'none'},{opacity:0,transform:'translateX('+(kind === 'library' ? -12 : 12)+'px)'}],{duration:140,easing:'cubic-bezier(.2,.7,.25,1)'});
    const record = {animation,ghost}; panelAnimations.set(kind,record);
    animation.finished.catch(()=>{}).finally(()=>{ghost.remove();if(panelAnimations.get(kind)===record)panelAnimations.delete(kind)});
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
    // The compact toolbar keeps secondary actions in an explicit menu at every width.
    const theme=byId('readerTheme'), themeTarget=innerWidth<=620?document.querySelector('#readerMore>.more-actions'):document.querySelector('.reader-tools');if(theme&&themeTarget&&theme.parentElement!==themeTarget)themeTarget.append(theme);const active = isReader(), source = isSource(), focus = active && reading.focus;
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
    if (!canMutate('preferences')) return Promise.resolve();
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
    const restoringPanels = reading.focus;
    if (!reading.focus) {if (visible('library')) animatePanel('library', false); if (visible('toc')) animatePanel('toc', false);}
    preservePosition(() => {
      if (reading.focus) leaveFocus();
      else {reading.focusRestore = {library: reading.library, toc: reading.toc}; reading.focus = true;}
      applyReading();
    });
    if (restoringPanels) {if (visible('library')) animatePanel('library', true); if (visible('toc')) animatePanel('toc', true);}
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
  const visibleFocusTarget = node => node?.isConnected && !node.inert && !node.disabled && node.matches('button,a[href],summary,input,select,textarea,[tabindex]') && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
  const visibleController = kind => (kind === 'library' ? [byId('toggleLibrary'),byId('headerNav'),byId('homeNav')] : [byId('toggleToc')]).find(visibleFocusTarget);
  function openDrawer(kind, trigger, search = false) {
    const activePanel = panel(kind);
    if (!activePanel || !isDrawer(kind)) return;
    closeDrawer(false);
    drawer = kind; drawerOpener = visibleFocusTarget(trigger) ? trigger : visibleController(kind) || document.activeElement;
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
    if (returnFocus && !document.querySelector('dialog[open]')) {
      const target=[opener,visibleController(previous),byId('toggleLibrary'),byId('headerNav'),byId('backHome'),byId('homeButton')].find(visibleFocusTarget);
      target?.focus({preventScroll:true});
    }
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
      if (isDrawer('library')) openDrawer('library', visibleController('library'), true);
      else {
        if (!visible('library')) {preservePosition(() => {leaveFocus(); reading.library = true; applyReading();}); persist();}
        byId('search').focus({preventScroll: true});
      }
    }
    if (event.key === 'Escape') {
      const menu = byId('readingSettings')?.open ? byId('readingSettings') : byId('readerMore')?.open ? byId('readerMore') : byId('reportIdentity')?.open ? byId('reportIdentity') : null;
      if (menu) {event.preventDefault(); menu.open = false; menu.querySelector('summary')?.focus({preventScroll: true});}
    }
  }, true);
  document.addEventListener('click', event => {
    const more = byId('readerMore'), settings = byId('readingSettings'), identity=byId('reportIdentity'); if(identity&&!identity.contains(event.target))identity.open=false;
    if (more && (!more.contains(event.target) || event.target.closest('#manageButton,#toBottom,#figuresButton,#readerImport,#readerStorage,#readerTheme,.export-menu a'))) more.open = false;
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
  // Native details own semantics and input. Motion is visual-only and never delays open/closed geometry.
  const menuRecords = new Map(), chromeAnimations = new Set();
  let chromeFrame = null, chromeVersion = 0;
  const rectCopy = r => ({left:r.left,top:r.top,width:r.width,height:r.height});
  const shape = (from,to) => 'translate('+(from.left-to.left)+'px,'+(from.top-to.top)+'px) scale('+Math.max(.02,from.width/to.width)+','+Math.max(.02,from.height/to.height)+')';
  function cancelMenu(record) {
    if(record.frame!==undefined)cancelAnimationFrame(record.frame);
    delete record.frame;record.animation?.cancel();record.animation=null;record.ghost?.remove();record.ghost=null;
  }
  function rememberMenu(details) {
    const record=menuRecords.get(details), surface=details.children[1];
    if(!record||!details.open||!surface?.getClientRects().length)return;
    if(!record.animation)record.full=rectCopy(surface.getBoundingClientRect());
    record.snapshot=surface.cloneNode(true);
    const css=getComputedStyle(surface);
    record.appearance={padding:css.padding,background:css.backgroundColor,color:css.color,border:css.border,borderRadius:css.borderRadius,boxShadow:css.boxShadow,display:css.display,flexDirection:css.flexDirection,alignItems:css.alignItems,gap:css.gap};
    record.visual=rectCopy(surface.getBoundingClientRect());
  }
  const rememberMenus=()=>{for(const details of menuRecords.keys())rememberMenu(details)};
  document.addEventListener('click',rememberMenus,true);
  window.addEventListener('keydown',rememberMenus,true);
  function animateMenu(details) {
    const record=menuRecords.get(details), surface=details.children[1], trigger=details.firstElementChild;
    if(!surface||!trigger)return;
    const interrupted=record.ghost?.isConnected?rectCopy(record.ghost.getBoundingClientRect()):null;
    const priorVisual=record.visual;
    cancelMenu(record);surface.style.animation='none';surface.style.transformOrigin='0 0';
    if(motionPreference.matches){if(details.open)rememberMenu(details);return}
    if(details.open) {
      const full=rectCopy(surface.getBoundingClientRect());if(!full.width||!full.height)return;
      record.full=full;rememberMenu(details);
      const from=interrupted||rectCopy(trigger.getBoundingClientRect());
      const animation=surface.animate([{transform:shape(from,full),opacity:.48,borderRadius:'24px'},{transform:'none',opacity:1,borderRadius:'18px'}],{duration:210,easing:'cubic-bezier(.16,1,.3,1)'});
      record.animation=animation;
      const track=()=>{if(record.animation!==animation||!details.open)return;record.visual=rectCopy(surface.getBoundingClientRect());record.frame=requestAnimationFrame(track)};
      track();animation.finished.catch(()=>{}).finally(()=>{if(record.animation===animation){record.animation=null;if(record.frame!==undefined)cancelAnimationFrame(record.frame);delete record.frame;if(details.open)rememberMenu(details)}});
      return;
    }
    const full=record.full;if(!full||!record.snapshot)return;
    const ghost=record.snapshot.cloneNode(true);ghost.removeAttribute('id');ghost.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));ghost.inert=true;ghost.setAttribute('aria-hidden','true');ghost.classList.add('menu-exit');
    Object.assign(ghost.style,record.appearance,{position:'fixed',left:full.left+'px',top:full.top+'px',width:full.width+'px',height:full.height+'px',maxHeight:'none',maxWidth:'none',margin:'0',animation:'none',transformOrigin:'0 0',overflow:'hidden',pointerEvents:'none',zIndex:'44'});
    document.body.append(ghost);record.ghost=ghost;
    const to=rectCopy(trigger.getBoundingClientRect()), from=priorVisual||full;
    const animation=ghost.animate([{transform:shape(from,full),opacity:1,borderRadius:'18px'},{transform:shape(to,full),opacity:0,borderRadius:'24px'}],{duration:160,easing:'cubic-bezier(.4,0,1,1)',fill:'forwards'});record.animation=animation;
    animation.finished.catch(()=>{}).finally(()=>{ghost.remove();if(record.animation===animation){record.animation=null;record.ghost=null}});
  }
  for(const details of [byId('readerMore'),byId('readingSettings'),byId('reportIdentity')].filter(Boolean)) {
    menuRecords.set(details,{});details.addEventListener('toggle',()=>animateMenu(details));
  }
  function cancelChrome() {if(chromeFrame!==null)cancelAnimationFrame(chromeFrame);chromeFrame=null;for(const animation of chromeAnimations)animation.cancel();chromeAnimations.clear()}
  motionPreference.addEventListener('change',event=>{if(event.matches){chromeVersion++;cancelChrome();for(const record of menuRecords.values())cancelMenu(record)}});
  window.addEventListener('pagehide',()=>{chromeVersion++;cancelChrome();for(const record of menuRecords.values())cancelMenu(record)});
  window.archiveReading={pageTransition(next){
    const prior=root.dataset.page;if(prior===next)return;
    const pairs=next==='reader'?[['headerNav','toggleLibrary'],['homeButton','reportIdentity'],['themeButton','readerTheme'],['importButton','readerMore']]:[['toggleLibrary','headerNav'],['reportIdentity','homeButton'],['readerTheme','themeButton'],['readerMore','importButton']];
    const from=pairs.map(([source,target])=>{const node=byId(source),r=node?.getBoundingClientRect();return{target,rect:r?.width?rectCopy(r):null}});
    const version=++chromeVersion;cancelChrome();for(const [details,record] of menuRecords){cancelMenu(record);details.open=false}
    if(motionPreference.matches)return;
    chromeFrame=requestAnimationFrame(()=>{
      chromeFrame=null;if(version!==chromeVersion||root.dataset.page!==next||motionPreference.matches)return;
      for(const item of from){const node=byId(item.target);if(!node||!item.rect)continue;const to=node.getBoundingClientRect();if(!to.width||!to.height)continue;
        const identity=['reportIdentity','homeButton'].includes(item.target),dx=identity?Math.max(-220,Math.min(220,item.rect.left-to.left)):item.rect.left-to.left,dy=item.rect.top-to.top;
        const transform='translate('+dx+'px,'+dy+'px)'+(identity?'':' scale('+item.rect.width/to.width+','+item.rect.height/to.height+')');
        const animation=node.animate([{transform,opacity:identity ? .3 : .65},{transform:'none',opacity:1}],{duration:identity?240:220,easing:'cubic-bezier(.16,1,.3,1)'});chromeAnimations.add(animation);animation.finished.catch(()=>{}).finally(()=>chromeAnimations.delete(animation));
      }
    });
  }};
  applyReading();
  Promise.resolve(window.archiveStartup || api('/api/library')).then(data => {reading = normalize(data.settings?.reading); committed = copy(reading);})
    .catch(error => toast('阅读设置暂未读取：' + error.message, 8000))
    .finally(() => {initialized = true; root.dataset.readingReady = 'true'; applyReading();});
})();
