(function () {
  'use strict';

  // --- Globals shared with inline script ---
  if (typeof window.flows === 'undefined') window.flows = [];
  if (typeof window.wizardFlowCounter === 'undefined') window.wizardFlowCounter = 0;

  const WIZARD_STEPS = 4;
  let flowSteps = [];
  let productFolderCache = [];

  function ensureFlowSteps() {
    while (flowSteps.length < flows.length) flowSteps.push(1);
  }

  function init() {
    if (!flows.length) addFlow();
    else ensureFlowSteps();
    renderAgentBoxes();
    refreshProductFolders();
    if (!productFolderCache.length && typeof window.loadFolderList === 'function') {
      window.loadFolderList();
    }
    setTimeout(retryStep1, 100);
    setTimeout(retryStep1, 700);
  }

  function retryStep1() {
    if (!productFolderCache.length) refreshProductFolders();
    for (let i = 0; i < flows.length; i++) {
      if ((flowSteps[i] || 1) === 1) renderStep1(i);
    }
  }

  // --- Override inline renderAgentBoxes so the wizard is shown ---
  window.renderAgentBoxes = function () {
    renderFlowBoxes();
  };

  // --- Override old handlers that no longer have DOM elements ---
  window.onConfirmClick = function () {};

  window.clearAll = function () {
    if (!confirm('ล้างทุก flow ใช่ไหม?')) return;
    flows = [];
    flowSteps = [];
    wizardFlowCounter = 0;
    addFlow();
    renderFlowBoxes();
  };

  // Keep old confirmAndRun but redirect to flow runner (all flows)
  window.confirmAndRun = async function () {
    await confirmAndRunFlows();
  };

  // --- Flow box rendering ---
  function _isFlowRunning(idx) {
    const nextBtn = document.getElementById('flow-next-' + idx);
    return !!(nextBtn && nextBtn.classList.contains('running'));
  }

  function _isFlowActive(idx) {
    const flow = flows[idx];
    if (!flow) return false;
    return _isFlowRunning(idx) || flow.finished;
  }

  function renderFlowBoxes() {
    const list = document.getElementById('flow-wizard-list');
    if (!list) return;
    // หา/สร้างปุ่ม "เพิ่ม Flow" — ต้องมีอยู่เสมอ
    let addBtn = list.querySelector('.flow-box.add-new');
    if (!addBtn) {
      addBtn = document.createElement('div');
      addBtn.className = 'flow-box add-new';
      addBtn.style.cssText = 'text-align:center;border-style:dashed;cursor:pointer;padding:16px 20px;';
      addBtn.textContent = '+ เพิ่ม Flow';
      addBtn.onclick = function () { window.addFlow(); };
      list.appendChild(addBtn);
    }
    // ลบ flow box เดิมที่ไม่ได้รัน/เสร็จแล้วออกก่อน (คงไว้เฉพาะที่กำลังรันหรือทำเสร็จแล้ว)
    const existingBoxes = list.querySelectorAll('.flow-box:not(.add-new)');
    existingBoxes.forEach((box) => {
      const idx = parseInt(box.dataset.flowIdx);
      if (!isNaN(idx) && _isFlowActive(idx)) return;  // อย่าลบ flow ที่กำลังรันหรือเสร็จแล้ว
      box.remove();
    });
    // แทรก flow ใหม่/ที่ยังไม่ได้ render ก่อนปุ่ม "เพิ่ม Flow"
    for (let i = 0; i < flows.length; i++) {
      if (document.getElementById('flow-box-' + i)) continue;  // มีอยู่แล้ว
      const wrapper = document.createElement('div');
      wrapper.innerHTML = renderFlowBox(i);
      const newBox = wrapper.firstElementChild;
      addBtn.before(newBox);
      if (flows[i].finished) {
        // flow ที่จบแล้ว → ซ่อนปุ่มยืนยัน/กลับ แสดงแค่ ×
        const nextBtn = document.getElementById('flow-next-' + i);
        const backBtn = document.getElementById('flow-back-' + i);
        const removeBtn = document.getElementById('flow-remove-' + i);
        if (nextBtn) nextBtn.style.display = 'none';
        if (backBtn) backBtn.style.display = 'none';
        if (removeBtn) removeBtn.style.display = 'inline-block';
      } else {
        setWizardStep(i, flowSteps[i] || 1);
      }
    }
  }

  function stepLabel(s) {
    if (s === 1) return 'สินค้า';
    if (s === 2) return 'Agent';
    if (s === 3) return 'ตัวเลือก';
    return 'ตรวจทาน';
  }

  function renderFlowBox(idx) {
    const flow = flows[idx];
    if (!flow) return '';

    let html = '<div class="flow-box" id="flow-box-' + idx + '" data-flow-idx="' + idx + '">';

    // Header
    html += '<div class="flow-box-header">';
    html += '<span class="flow-box-title">Flow ' + (idx + 1) + '</span>';
    html += '<span class="flow-box-remove" id="flow-remove-' + idx + '" data-flow-idx="' + idx + '" onclick="removeFlow(this.dataset.flowIdx, event)">×</span>';
    html += '</div>';

    // Stepper
    html += '<div class="wizard-stepper" id="flow-stepper-' + idx + '">';
    for (let s = 1; s <= WIZARD_STEPS; s++) {
      html += '<div class="wizard-step-dot" id="flow-step-dot-' + idx + '-' + s + '" data-step="' + s + '"><span class="num">' + s + '</span><span>' + stepLabel(s) + '</span></div>';
      if (s < WIZARD_STEPS) {
        html += '<div class="wizard-step-line" id="flow-step-line-' + idx + '-' + s + '" data-line="' + s + '"></div>';
      }
    }
    html += '</div>';

    // Step cards
    for (let s = 1; s <= WIZARD_STEPS; s++) {
      html += '<div class="wizard-card" id="flow-wizard-card-' + idx + '-' + s + '">';
      html += renderStepCardShell(idx, s);
      html += '</div>';
    }

    // Nav
    html += '<div class="wizard-nav flow-box-nav" style="align-items:center">';
    html += '<button class="btn btn-secondary" id="flow-back-' + idx + '" data-flow-idx="' + idx + '" onclick="wizardPrevStep(this.dataset.flowIdx)">← กลับ</button>';
    html += '<input type="text" id="flow-quick-brief-' + idx + '" data-flow-idx="' + idx + '" value="' + escapeHtml(flow.quickBrief || '') + '" ' + (_isFlowActive(idx) ? 'disabled ' : '') + 'oninput="onWizardQuickBrief(parseInt(this.dataset.flowIdx))" placeholder="คำสั่งเพิ่มเติมสำหรับ flow นี้..." style="flex:1;min-width:0;background:#161922;border:1px solid #252a3a;border-radius:6px;padding:6px 10px;color:#e4e4e7;font-size:13px;margin:0 8px;">';
    html += '<button class="btn btn-secondary" id="flow-schedule-' + idx + '" data-flow-idx="' + idx + '" onclick="openScheduleModal(this.dataset.flowIdx)">📅 ตั้งเวลา</button>';
    html += '<button class="btn btn-primary" id="flow-next-' + idx + '" data-flow-idx="' + idx + '" onclick="wizardNextStep(this.dataset.flowIdx)">ถัดไป →</button>';
    html += '</div>';

    html += '</div>';
    return html;
  }

  function renderStepCardShell(idx, s) {
    if (s === 1) {
      return '<div class="wizard-card-title">เลือกสินค้า</div>' +
        '<div class="wizard-card-subtitle">เลือกสินค้าหรือให้ AI เลือกอัตโนมัติ</div>' +
        '<div class="product-grid" id="flow-product-grid-' + idx + '"></div>' +
        '<div id="flow-selected-summary-' + idx + '" style="margin-top:12px"></div>' +
        '<div id="flow-auto-controls-' + idx + '" style="margin-top:12px; display:none">' +
          '<label style="font-size:13px;color:#a1a1aa;margin-right:8px">โหมด Auto:</label>' +
          '<select id="flow-auto-combined-' + idx + '" data-flow-idx="' + idx + '" onchange="onWizardAutoChange(this)" style="padding:6px 10px;border-radius:6px;border:1px solid #252a3a;background:#161922;color:#e4e4e7">' +
            '<option value="separate">1 สินค้า</option>' +
            '<option value="combined">รวมหลายสินค้า</option>' +
          '</select>' +
          '<span id="flow-auto-count-wrap-' + idx + '" style="display:none;margin-left:8px">' +
            '<label style="font-size:13px;color:#a1a1aa;margin-right:6px">จำนวน</label>' +
            '<input type="number" id="flow-auto-count-' + idx + '" data-flow-idx="' + idx + '" onchange="onWizardAutoChange(this)" min="2" max="10" value="2" style="width:60px;padding:6px;border-radius:6px;border:1px solid #252a3a;background:#161922;color:#e4e4e7">' +
          '</span>' +
        '</div>';
    }
    if (s === 2) {
      return '<div class="wizard-card-title">เลือก Agent</div>' +
        '<div class="wizard-card-subtitle">ลาก ☰ ข้างหน้าเพื่อเรียงลำดับ agent</div>' +
        '<div id="flow-agent-list-' + idx + '"></div>' +
        '<div class="add-agent-chips" id="flow-add-agent-chips-' + idx + '"></div>';
    }
    if (s === 3) {
      return '<div class="wizard-card-title">ตัวเลือกการสร้างคอนเทนต์</div>' +
        '<div class="wizard-card-subtitle">ตัวเลือกสำหรับ content_creator</div>' +
        '<div id="flow-opts-content-' + idx + '"></div>';
    }
    return '<div class="wizard-card-title">ตรวจทานและยืนยัน</div>' +
      '<div class="wizard-card-subtitle">ตรวจสอบก่อนรัน</div>' +
      '<div id="flow-review-content-' + idx + '"></div>' +
      '<div class="flow-display" id="flow-display-' + idx + '" style="margin-top:20px; display:none"></div>';
  }

  // --- Stepper ---
  window.setWizardStep = function (idx, step) {
    idx = parseInt(idx) || 0;
    step = Math.max(1, Math.min(WIZARD_STEPS, step || 1));
    flowSteps[idx] = step;

    // dots
    for (let s = 1; s <= WIZARD_STEPS; s++) {
      const dot = document.getElementById('flow-step-dot-' + idx + '-' + s);
      if (!dot) continue;
      dot.classList.remove('active', 'done', 'pending');
      if (s < step) dot.classList.add('done');
      else if (s === step) dot.classList.add('active');
      else dot.classList.add('pending');
    }
    // lines
    for (let s = 1; s < WIZARD_STEPS; s++) {
      const line = document.getElementById('flow-step-line-' + idx + '-' + s);
      if (line) line.classList.toggle('done', s < step);
    }
    // cards
    for (let s = 1; s <= WIZARD_STEPS; s++) {
      const card = document.getElementById('flow-wizard-card-' + idx + '-' + s);
      if (card) card.classList.toggle('active', s === step);
    }
    // nav buttons
    const backBtn = document.getElementById('flow-back-' + idx);
    const nextBtn = document.getElementById('flow-next-' + idx);
    if (backBtn) backBtn.style.display = step === 1 ? 'none' : 'inline-block';
    if (nextBtn) {
      nextBtn.textContent = step === WIZARD_STEPS ? '✓ ยืนยัน' : 'ถัดไป →';
      nextBtn.disabled = !wizardCanNext(idx);
    }
    const scheduleBtn = document.getElementById('flow-schedule-' + idx);
    if (scheduleBtn) scheduleBtn.style.display = step === WIZARD_STEPS ? 'inline-block' : 'none';

    if (step === 1) renderStep1(idx);
    if (step === 2) renderStep2(idx);
    if (step === 3) renderStep3(idx);
    if (step === 4) renderStep4(idx);
  };

  function wizardCanNext(idx) {
    const flow = flows[idx];
    if (!flow) return false;
    const step = flowSteps[idx] || 1;
    if (step === 1) return flow.isAuto ? true : flow.products.length > 0;
    if (step === 2) return flow.agents.length > 0;
    return true;
  }

  window.wizardNextStep = function (idx) {
    idx = parseInt(idx) || 0;
    const nextBtn = document.getElementById('flow-next-' + idx);
    if (nextBtn && nextBtn.classList.contains('running')) {
      stopAll();
      return;
    }
    if (!wizardCanNext(idx)) return;
    const step = flowSteps[idx] || 1;
    if (step === WIZARD_STEPS) {
      window.runSingleFlow(idx);
      return;
    }
    setWizardStep(idx, step + 1);
  };

  window.wizardPrevStep = function (idx) {
    idx = parseInt(idx) || 0;
    const step = flowSteps[idx] || 1;
    if (step > 1) setWizardStep(idx, step - 1);
  };

  // --- Flows ---
  function getCurrentFlow(idx) {
    return flows[idx] || null;
  }

  const MAX_AGENTS_PER_FLOW = 1;  // TEMP LOCK: แต่ละ flow รัน agent เดียว

  function normalizeAgents(agents) {
    // TEMP LOCK: รองรับอนาคต multi-agent แต่ตอนนี้ตัดเหลือ 1 ตัว
    return agents.slice(0, MAX_AGENTS_PER_FLOW);
  }

  function addFlow() {
    wizardFlowCounter++;
    const flow = {
      id: wizardFlowCounter,
      isAuto: false,
      finished: false,
      autoCombined: false,
      autoCount: 2,
      products: [],
      quickBrief: '',
      agents: normalizeAgents(['content_creator']),
      options: {
        platform: ['facebook', 'tiktok'],
        count: 1,
        media_type: 'image',
        media_when: 'auto',
        auto_image: true,
        auto_video: false
      }
    };
    flows.push(flow);
    flowSteps.push(1);
  }

  function removeFlow(idx) {
    if (flows.length <= 1) return;
    // ห้ามลบ flow ที่กำลังรันอยู่
    if (_isFlowRunning(idx)) return;
    flows.splice(idx, 1);
    flowSteps.splice(idx, 1);
    renderFlowBoxes();
  }

  window.addFlow = function () {
    addFlow();
    renderFlowBoxes();
  };

  window.removeFlow = function (idx, ev) {
    if (ev) ev.stopPropagation();
    removeFlow(parseInt(idx) || 0);
  };

  window.switchFlow = function () {};

  // --- Step 1: Products ---
  function refreshProductFolders() {
    const sidebar = document.getElementById('sidebar-content');
    if (!sidebar) return;
    const cards = sidebar.querySelectorAll('[data-folder]');
    productFolderCache = [];
    cards.forEach(c => {
      productFolderCache.push({
        path: c.dataset.folder,
        name: c.dataset.fname || c.dataset.name || c.dataset.folder,
        status: c.dataset.status || 'ready',
        ready: c.dataset.status === 'ready' || c.dataset.status === 'stale'
      });
    });
  }

  function renderStep1(idx) {
    const grid = document.getElementById('flow-product-grid-' + idx);
    const summary = document.getElementById('flow-selected-summary-' + idx);
    const autoControls = document.getElementById('flow-auto-controls-' + idx);
    if (!grid) return;

    if (!productFolderCache.length) {
      grid.innerHTML = '<div style="grid-column:1/-1;color:#71717a;font-size:13px">ยังไม่มีสินค้า</div>';
      return;
    }

    const flow = getCurrentFlow(idx);
    if (!flow) return;

    let html = '';
    html += '<div class="product-card auto ' + (flow.isAuto ? 'selected' : '') + '" data-flow-idx="' + idx + '" data-folder="__auto__" onclick="onWizardProductClick(this.dataset.folder, parseInt(this.dataset.flowIdx))">';
    html += '<div class="icon">⚡</div>';
    html += '<div class="name">Auto</div>';
    html += '<div class="badge">AI เลือกเอง</div>';
    html += '</div>';

    for (const f of productFolderCache) {
      const selected = !flow.isAuto && flow.products.includes(f.path);
      const disabled = !f.ready && !selected;
      html += '<div class="product-card ' + (selected ? 'selected' : '') + (disabled ? ' disabled' : '') + '" data-flow-idx="' + idx + '" data-folder="' + escapeHtml(f.path) + '"';
      html += disabled ? ' style="opacity:0.4;cursor:not-allowed"' : '';
      html += disabled ? '' : ' onclick="onWizardProductClick(this.dataset.folder, parseInt(this.dataset.flowIdx))"';
      html += '>';
      html += '<div class="icon">📦</div>';
      html += '<div class="name">' + escapeHtml(f.name) + '</div>';
      if (f.status === 'ready' || f.status === 'stale') html += '<div class="badge">พร้อม</div>';
      else html += '<div class="badge" style="color:#f59e0b">' + escapeHtml(f.status) + '</div>';
      html += '</div>';
    }
    grid.innerHTML = html;

    if (flow.isAuto) {
      const autoProductCount = flow.autoCombined ? flow.autoCount : 1;
      summary.innerHTML = '<span style="color:#818cf8">⚡ AI เลือก ' + autoProductCount + ' สินค้า ' + (flow.autoCombined ? 'รวมกัน' : 'ชิ้นเดียว') + '</span>';
    } else if (flow.products.length) {
      let chips = flow.products.map(p => '<span class="selected-chip' + (flow.products.length >= 2 ? ' combined' : '') + '">📦 ' + escapeHtml(p) + '</span>').join('');
      summary.innerHTML = '<span style="color:#71717a">เลือก:</span> ' + chips;
    } else {
      summary.innerHTML = '<span style="color:#71717a">ยังไม่ได้เลือกสินค้า</span>';
    }

    if (autoControls) {
      autoControls.style.display = flow.isAuto ? 'block' : 'none';
      const combined = document.getElementById('flow-auto-combined-' + idx);
      const count = document.getElementById('flow-auto-count-' + idx);
      const countWrap = document.getElementById('flow-auto-count-wrap-' + idx);
      const readyCount = productFolderCache.filter(f => f.ready).length;
      // ถ้ามีสินค้าพร้อม < 2 → ปิด combined, บังคับ separate
      if (readyCount < 2 && flow.autoCombined) {
        flow.autoCombined = false;
      }
      if (combined) {
        combined.value = flow.autoCombined ? 'combined' : 'separate';
        combined.disabled = readyCount < 2;
      }
      if (count) {
        count.max = Math.max(2, readyCount);
        count.value = Math.min(flow.autoCount, readyCount);
        flow.autoCount = parseInt(count.value) || 1;
      }
      if (countWrap) countWrap.style.display = flow.autoCombined ? 'inline-block' : 'none';
    }

    updateWizardNav(idx);
  }

  window.onWizardProductClick = function (folder, idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    if (folder === '__auto__') {
      flow.isAuto = !flow.isAuto;
      if (flow.isAuto) {
        flow.products = [];
        // Auto mode ไม่ล็อก agent แล้ว — user เลือกเองได้
      }
    } else {
      flow.isAuto = false;
      const i = flow.products.indexOf(folder);
      if (i >= 0) flow.products.splice(i, 1);
      else flow.products.push(folder);
    }
    renderStep1(idx);
  };

  window.onWizardAutoChange = function (el) {
    const idx = parseInt(el.dataset.flowIdx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    const combined = document.getElementById('flow-auto-combined-' + idx);
    const count = document.getElementById('flow-auto-count-' + idx);
    const countWrap = document.getElementById('flow-auto-count-wrap-' + idx);
    const readyCount = productFolderCache.filter(f => f.ready).length;
    if (combined) flow.autoCombined = combined.value === 'combined' && readyCount >= 2;
    if (count && flow.autoCombined) flow.autoCount = Math.max(2, Math.min(readyCount, parseInt(count.value) || 2));
    if (countWrap) countWrap.style.display = flow.autoCombined ? 'inline-block' : 'none';
    renderStep1(idx);
  };

  // --- Step 2: Agents ---
  function renderStep2(idx) {
    const el = document.getElementById('flow-agent-list-' + idx);
    const chipsEl = document.getElementById('flow-add-agent-chips-' + idx);
    if (!el) return;
    const flow = getCurrentFlow(idx);

    let html = '';
    for (let i = 0; i < flow.agents.length; i++) {
      const key = flow.agents[i];
      const info = AGENT_INFO[key];
      html += '<div class="agent-row" data-flow-idx="' + idx + '" data-agent-idx="' + i + '" ondragover="onWizardAgentDragOver(event, this)" ondragleave="onWizardAgentDragLeave(event, this)" ondrop="onWizardAgentDrop(event, this)">';
      html += '<div class="drag-handle" draggable="true" title="ลากจัดเรียง" ondragstart="onWizardAgentDragStart(event, this)" ondragend="onWizardAgentDragEnd(event, this)">☰</div>';
      html += '<div class="order-num">' + (i + 1) + '</div>';
      html += '<div class="agent-icon">' + info.icon + '</div>';
      html += '<div class="agent-text">';
      html += '<div class="agent-name">' + info.name + '</div>';
      html += '<div class="agent-desc">' + info.flow_reason + '</div>';
      html += '</div>';
      html += '<button class="settings-btn" onclick="openAgentSettings(\'' + escapeJs(key) + '\')" title="ตั้งค่า">⚙</button>';
      html += '<button class="remove-btn" data-flow-idx="' + idx + '" data-agent-idx="' + i + '" onclick="removeWizardAgent(parseInt(this.dataset.agentIdx), parseInt(this.dataset.flowIdx))" title="ลบ">×</button>';
      html += '</div>';
    }
    el.innerHTML = html;

    // เพิ่ม Agent แบบ chips (แสดงเฉพาะ agent ที่ยังไม่อยู่ใน flow)
    // TEMP LOCK: แต่ละ flow อนุญาต agent เดียว — ซ่อน chips ถ้ามี agent แล้ว
    if (chipsEl) {
      if (flow.agents.length >= 1) {
        chipsEl.innerHTML = '';
      } else {
        const missing = typeof AGENT_ORDER !== 'undefined' ? AGENT_ORDER.filter(k => !flow.agents.includes(k)) : [];
        if (missing.length) {
          let cHtml = '<span style="font-size:13px;color:#71717a;width:100%;margin-bottom:4px">เพิ่ม Agent:</span>';
          for (const k of missing) {
            const info = AGENT_INFO[k] || {};
            cHtml += '<div class="add-agent-chip" data-flow-idx="' + idx + '" data-agent-key="' + escapeHtml(k) + '" onclick="onWizardAgentChipClick(this.dataset.agentKey, parseInt(this.dataset.flowIdx))">';
            cHtml += (info.icon || '') + ' ' + (info.name || k);
            cHtml += '</div>';
          }
          chipsEl.innerHTML = cHtml;
        } else {
          chipsEl.innerHTML = '<span class="add-agent-empty">ครบทุก agent แล้ว</span>';
        }
      }
    }

    updateWizardNav(idx);
  }

  let _wizardAgentDrag = null;

  function _agentRow(el) {
    return el && el.closest ? (el.closest('.agent-row') || el) : el;
  }

  window.onWizardAgentDragStart = function (ev, el) {
    const row = el && el.closest ? el.closest('.agent-row') : null;
    if (!row) return;
    _wizardAgentDrag = { flowIdx: parseInt(row.dataset.flowIdx), agentIdx: parseInt(row.dataset.agentIdx) };
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = 'move';
      try {
        const hRect = el.getBoundingClientRect();
        const rRect = row.getBoundingClientRect();
        ev.dataTransfer.setDragImage(row, hRect.left - rRect.left, hRect.top - rRect.top);
      } catch (e) {}
    }
    row.classList.add('dragging');
  };

  window.onWizardAgentDragOver = function (ev, el) {
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
    const row = _agentRow(el);
    if (row) row.classList.add('drag-over');
  };

  window.onWizardAgentDragLeave = function (ev, el) {
    const row = _agentRow(el);
    if (!row) return;
    const related = ev && ev.relatedTarget;
    if (!related || !row.contains(related)) {
      row.classList.remove('drag-over');
    }
  };

  window.onWizardAgentDrop = function (ev, el) {
    ev.preventDefault();
    if (!_wizardAgentDrag) return;
    const row = _agentRow(el);
    if (!row) return;
    const flowIdx = parseInt(row.dataset.flowIdx);
    const agentIdx = parseInt(row.dataset.agentIdx);
    if (flowIdx !== _wizardAgentDrag.flowIdx) return;
    if (agentIdx === _wizardAgentDrag.agentIdx) return;
    const flow = getCurrentFlow(flowIdx);
    if (!flow) return;
    const src = _wizardAgentDrag.agentIdx;
    const dst = agentIdx;
    const tmp = flow.agents[src];
    flow.agents[src] = flow.agents[dst];
    flow.agents[dst] = tmp;
    _wizardAgentDrag = null;
    renderStep2(flowIdx);
  };

  window.onWizardAgentDragEnd = function (ev, el) {
    const row = _agentRow(el);
    if (row) row.classList.remove('dragging');
    document.querySelectorAll('.agent-row').forEach(r => r.classList.remove('drag-over'));
    _wizardAgentDrag = null;
  };

  window.onWizardAgentChipClick = function (key, idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow || !key) return;
    // TEMP LOCK: แต่ละ flow อนุญาต agent เดียว
    if (flow.agents.length >= 1) return;
    if (!flow.agents.includes(key)) flow.agents.push(key);
    renderStep2(idx);
  };

  window.removeWizardAgent = function (idx, flowIdx) {
    flowIdx = parseInt(flowIdx);
    const flow = getCurrentFlow(flowIdx);
    if (!flow) return;
    flow.agents.splice(idx, 1);
    renderStep2(flowIdx);
  };

  // --- Step 3: Options ---
  function renderStep3(idx) {
    const el = document.getElementById('flow-opts-content-' + idx);
    if (!el) return;
    const flow = getCurrentFlow(idx);
    const o = flow.options;
    const hasContentCreator = flow.agents.includes('content_creator');
    if (!hasContentCreator) {
      el.innerHTML = '<div style="color:#71717a;font-size:13px">ไม่มี content_creator ใน flow — ไม่ต้องตั้งค่า</div>';
      return;
    }
    let html = '';
    html += '<div class="opt-group">';
    html += '<label class="opt-label">แพลตฟอร์ม</label>';
    html += '<div class="opt-chips">';
    const platforms = ['facebook', 'tiktok'];
    const names = { facebook: '📘 Facebook', tiktok: '🎵 TikTok' };
    for (const p of platforms) {
      const active = o.platform.includes(p) ? 'active' : '';
      html += '<span class="opt-chip ' + active + '" data-flow-idx="' + idx + '" data-platform="' + p + '" onclick="onWizardPlatformClick(this.dataset.platform, parseInt(this.dataset.flowIdx))">' + names[p] + '</span>';
    }
    html += '</div></div>';

    html += '<div class="opt-group">';
    html += '<label class="opt-label">จำนวนโพสต์ต่อแพลตฟอร์ม</label>';
    html += '<div class="opt-input-row">';
    html += '<input type="number" id="flow-opt-count-' + idx + '" data-flow-idx="' + idx + '" onchange="onWizardCountChange(parseInt(this.dataset.flowIdx))" value="' + o.count + '" min="1" max="20" style="width:80px;padding:6px;border-radius:6px;border:1px solid #252a3a;background:#161922;color:#e4e4e7">';
    html += '<span style="font-size:13px;color:#71717a">โพสต์</span>';
    html += '</div>';
    html += '<div style="font-size:12px;color:#71717a;margin-top:4px">รวม ' + (o.count * o.platform.length) + ' โพสต์</div>';
    html += '</div>';

    html += '<div class="opt-group">';
    html += '<label class="opt-label">สื่อที่ต้องการสร้าง</label>';
    html += '<div class="opt-chips">';
    html += '<span class="opt-chip ' + (o.auto_image ? 'active' : '') + '" data-flow-idx="' + idx + '" onclick="onWizardAutoImageToggle(parseInt(this.dataset.flowIdx))">🖼 รูป</span>';
    html += '<span class="opt-chip ' + (o.auto_video ? 'active' : '') + '" data-flow-idx="' + idx + '" onclick="onWizardAutoVideoToggle(parseInt(this.dataset.flowIdx))">🎥 วิดีโอ</span>';
    html += '</div>';
    html += '</div>';

    html += '<div class="opt-group">';
    html += '<div class="opt-input-row" style="gap:8px;align-items:center">';
    html += '<input type="checkbox" id="flow-opt-ask-' + idx + '" data-flow-idx="' + idx + '" onchange="onWizardMediaWhenToggle(parseInt(this.dataset.flowIdx))" ' + (o.media_when === 'ask' ? 'checked' : '') + ' style="width:16px;height:16px;cursor:pointer">';
    html += '<label for="flow-opt-ask-' + idx + '" style="font-size:13px;color:#a1a1aa;cursor:pointer">ถามก่อนสร้างสื่อ (ถ้าไม่เลือกจะสร้างทันทีหลังเสร็จ)</label>';
    html += '</div></div>';

    el.innerHTML = html;
  }

  window.onWizardPlatformClick = function (p, idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    const i = flow.options.platform.indexOf(p);
    if (i >= 0) {
      if (flow.options.platform.length > 1) flow.options.platform.splice(i, 1);
    } else {
      flow.options.platform.push(p);
    }
    renderStep3(idx);
  };

  window.onWizardCountChange = function (idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    const el = document.getElementById('flow-opt-count-' + idx);
    if (el) flow.options.count = Math.max(1, Math.min(20, parseInt(el.value) || 1));
    renderStep3(idx);
  };

  function _syncMediaType(flow) {
    if (flow.options.auto_image && flow.options.auto_video) flow.options.media_type = 'both';
    else if (flow.options.auto_video) flow.options.media_type = 'video';
    else flow.options.media_type = 'image';
  }

  window.onWizardMediaWhenToggle = function (idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    flow.options.media_when = flow.options.media_when === 'ask' ? 'auto' : 'ask';
    renderStep3(idx);
  };

  window.onWizardAutoImageToggle = function (idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    flow.options.auto_image = !flow.options.auto_image;
    if (!flow.options.auto_image && !flow.options.auto_video) flow.options.auto_image = true;
    _syncMediaType(flow);
    renderStep3(idx);
  };

  window.onWizardAutoVideoToggle = function (idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    flow.options.auto_video = !flow.options.auto_video;
    if (!flow.options.auto_image && !flow.options.auto_video) flow.options.auto_video = true;
    _syncMediaType(flow);
    renderStep3(idx);
  };

  window.onWizardQuickBrief = function (idx) {
    idx = parseInt(idx);
    const flow = getCurrentFlow(idx);
    if (!flow) return;
    const el = document.getElementById('flow-quick-brief-' + idx);
    if (el) flow.quickBrief = el.value;
  };

  window.setQuickBriefDisabled = function (idx, disabled) {
    const el = document.getElementById('flow-quick-brief-' + idx);
    if (el) el.disabled = disabled;
  };

  // --- Step 4: Review ---
  function renderStep4(idx) {
    const el = document.getElementById('flow-review-content-' + idx);
    if (!el) return;
    const f = flows[idx];
    if (!f) return;

    let html = '';
    html += '<div class="review-flow">';
    html += '<div class="review-flow-header">';
    html += '<span class="review-flow-title">Flow ' + (idx + 1) + '</span>';
    let badgeClass = 'separate', badgeText = 'แยกสินค้า';
    if (f.isAuto) { badgeClass = 'auto'; badgeText = '⚡ Auto'; }
    else if (f.products.length >= 2) { badgeClass = 'combined'; badgeText = 'รวม ' + f.products.length + ' สินค้า'; }
    else if (f.products.length === 1) { badgeClass = 'separate'; badgeText = 'สินค้าเดียว'; }
    html += '<span class="review-flow-badge ' + badgeClass + '">' + badgeText + '</span>';
    html += '</div>';

    html += '<div style="font-size:13px;color:#a1a1aa;margin-bottom:10px">สินค้า: ';
    if (f.isAuto) html += '<span style="color:#818cf8">AI เลือก ' + (f.autoCombined ? f.autoCount : 1) + ' ชิ้น</span>';
    else html += f.products.map(p => escapeHtml(p)).join(' + ') || '<span style="color:#ef4444">ยังไม่เลือก</span>';
    html += '</div>';

    html += '<div class="review-steps">';
    for (let j = 0; j < f.agents.length; j++) {
      const key = f.agents[j];
      html += '<div class="review-step">';
      html += '<span>' + AGENT_INFO[key].icon + ' ' + AGENT_INFO[key].name + '</span>';
      if (j < f.agents.length - 1) html += '<span class="review-arrow">→</span>';
      html += '</div>';
    }
    html += '</div>';

    if (f.agents.includes('content_creator')) {
      html += '<div class="review-opts">';
      html += 'แพลตฟอร์ม: ' + f.options.platform.join(' / ') + ' · ';
      html += 'จำนวน: ' + f.options.count + ' โพสต์/แพลตฟอร์ม (รวม ' + (f.options.count * f.options.platform.length) + ') · ';
      html += 'สื่อ: ' + f.options.media_type + ' · ';
      html += f.options.media_when === 'auto' ? 'สร้างทันที' : 'ถามก่อนสร้าง';
      html += '</div>';
    }

    html += '</div>';
    el.innerHTML = html;
  }

  function updateWizardNav(idx) {
    const nextBtn = document.getElementById('flow-next-' + idx);
    if (nextBtn) nextBtn.disabled = !wizardCanNext(idx);
  }

  // --- Execution ---
  function buildFlowsForBackend() {
    return flows.map((f, i) => ({
      index: i,
      folders: f.isAuto ? [] : f.products,
      agents: f.agents,
      is_auto: f.isAuto,
      auto_combined: f.autoCombined,
      auto_count: f.autoCount,
      content_count: f.options.count * f.options.platform.length,
      quick_brief: f.quickBrief || '',
      platforms: f.options.platform,
      media_type: f.options.media_type,
      media_when: f.options.media_when,
      auto_image: f.options.media_when === 'auto' ? f.options.auto_image : false,
      auto_video: f.options.media_when === 'auto' ? f.options.auto_video : false
    }));
  }

  function showFlowRuns(onlyIdx) {
    for (let i = 0; i < flows.length; i++) {
      if (typeof onlyIdx === 'number' && i !== onlyIdx) continue;
      const el = document.getElementById('flow-display-' + i);
      if (!el) continue;
      const f = flows[i];
      let html = '';
      html += '<div class="flow-product" id="flow-product-' + i + '">';
      html += '<div class="flow-product-title">Flow ' + (i + 1) + ': ' + (f.isAuto ? '⚡ Auto' : f.products.join(' + ')) + '</div>';
      html += '<div class="flow-steps">';
      if (f.isAuto) {
        html += '<div class="flow-step" id="flow-' + i + '-0">';
        html += '<div class="flow-step-header">';
        html += '<span class="flow-step-icon">⚡</span>';
        html += '<span class="flow-step-name">Auto</span>';
        html += '<span class="flow-spinner"></span>';
        html += '<span class="flow-step-badge" id="flow-' + i + '-0-status"></span>';
        html += '</div>';
        html += '<div class="flow-step-links" id="flow-' + i + '-0-links" style="margin-top:6px"></div>';
        html += '</div>';
      } else {
        for (let j = 0; j < f.agents.length; j++) {
          const key = f.agents[j];
          const info = AGENT_INFO[key];
          html += '<div class="flow-step" id="flow-' + i + '-' + j + '">';
          html += '<div class="flow-step-header">';
          html += '<span class="flow-step-icon">' + info.icon + '</span>';
          html += '<span class="flow-step-name">' + info.name + '</span>';
          html += '<span class="flow-spinner"></span>';
          html += '<span class="flow-step-badge" id="flow-' + i + '-' + j + '-status"></span>';
          html += '</div>';
          html += '<div class="flow-step-links" id="flow-' + i + '-' + j + '-links" style="margin-top:6px"></div>';
          html += '</div>';
          if (j < f.agents.length - 1) html += '<div class="flow-arrow">→</div>';
        }
      }
      html += '</div>';
      html += '</div>';
      if (typeof onlyIdx !== 'number') {
        html += '<div class="flow-explain">';
        html += '<div class="flow-explain-item"><b>รันตามลำดับ</b> ภายใน flow ผล agent ก่อนหน้าส่งต่อไป agent ถัดไป</div>';
        html += '<div class="flow-explain-item"><b>หลาย flow</b> รันขนานกัน</div>';
        html += '</div>';
      }
      el.innerHTML = html;
      el.style.display = 'block';
    }
  }

  async function confirmAndRunFlows() {
    if (!flows.length) return;

    const finished = flows.findIndex(f => f.finished);
    if (finished >= 0) {
      alert('Flow ' + (finished + 1) + ' ทำงานเสร็จแล้ว กรุณาเพิ่ม flow ใหม่หรือกด Reset');
      return;
    }

    const autoCount = flows.filter(f => f.isAuto).length;
    if (autoCount > 0 && autoCount !== flows.length) {
      alert('ยังไม่รองรับการผสม Auto กับ flow ปกติในรอบเดียว — กรุณาเลือกอย่างใดอย่างหนึ่ง');
      return;
    }

    const notReady = [];
    for (let i = 0; i < flows.length; i++) {
      const f = flows[i];
      if (f.isAuto) continue;
      const needsDb = f.agents.some(a => a !== 'product_spec');
      if (!needsDb) continue;
      for (const folder of f.products) {
        const status = await checkProductStatus(folder);
        if (status !== 'ready' && status !== 'stale') notReady.push({ flow: i + 1, folder, status });
      }
    }
    if (notReady.length) {
      const labels = { empty: 'ว่าง', pending: 'pending', no_usable_data: 'ไม่มีไฟล์', processing: 'กำลังประมวลผล' };
      const msg = notReady.map(n => '• Flow ' + n.flow + ' ' + n.folder + ': ' + (labels[n.status] || n.status)).join('\n');
      alert('สินค้ายังไม่พร้อม:\n\n' + msg);
      return;
    }

    abortController = new AbortController();
    showFlowRuns();

    for (let i = 0; i < flows.length; i++) {
      setQuickBriefDisabled(i, true);
    }

    if (autoCount > 0) {
      await runAutoFlow(flows[0], 0);
    } else {
      const payload = { flows: buildFlowsForBackend() };
      await runAllFlows(payload);
    }

    loadCredits();
  }

  window.runSingleFlow = async function (idx) {
    idx = parseInt(idx);
    const flow = flows[idx];
    if (!flow) return;
    if (flow.finished) {
      alert('Flow นี้ทำงานเสร็จแล้ว กรุณาเพิ่ม flow ใหม่หรือกด Reset');
      return;
    }

    const nextBtn = document.getElementById('flow-next-' + idx);
    const backBtn = document.getElementById('flow-back-' + idx);
    const removeBtn = document.getElementById('flow-remove-' + idx);
    if (nextBtn) { nextBtn.classList.add('running'); nextBtn.style.display = 'none'; }
    if (backBtn) backBtn.style.display = 'none';
    if (removeBtn) removeBtn.style.display = 'none';

    if (!flow.isAuto) {
      const needsDb = flow.agents.some(a => a !== 'product_spec');
      if (needsDb) {
        const notReady = [];
        for (const folder of flow.products) {
          const status = await checkProductStatus(folder);
          if (status !== 'ready' && status !== 'stale') notReady.push({ folder, status });
        }
        if (notReady.length) {
          const labels = { empty: 'ว่าง', pending: 'pending', no_usable_data: 'ไม่มีไฟล์', processing: 'กำลังประมวลผล' };
          const msg = notReady.map(n => '• ' + n.folder + ': ' + (labels[n.status] || n.status)).join('\n');
          alert('สินค้ายังไม่พร้อม:\n\n' + msg);
          if (nextBtn) { nextBtn.classList.remove('running'); nextBtn.style.display = 'inline-block'; nextBtn.textContent = '✓ ยืนยัน'; nextBtn.disabled = false; }
          if (backBtn) backBtn.style.display = 'inline-block';
          if (removeBtn) removeBtn.style.display = 'inline-block';
          return;
        }
      }
    }

    abortController = new AbortController();
    showFlowRuns(idx);
    setQuickBriefDisabled(idx, true);

    try {
      if (flow.isAuto) {
        await runAutoFlow(flow, idx);
      } else {
        const all = buildFlowsForBackend();
        const payload = { flows: [all[idx]] };
        await runAllFlows(payload);
      }
    } catch (e) {
      if (e.name !== 'AbortError') alert('ผิดพลาด: ' + e.message);
      // error → restore buttons so user can retry
      if (nextBtn) { nextBtn.classList.remove('running'); nextBtn.style.display = 'inline-block'; nextBtn.textContent = '✓ ยืนยัน'; nextBtn.disabled = false; }
      if (backBtn) backBtn.style.display = 'inline-block';
      if (removeBtn) removeBtn.style.display = 'inline-block';
    } finally {
      if (flow.finished) {
        // done → hide next/back, show × only
        if (nextBtn) nextBtn.style.display = 'none';
        if (backBtn) backBtn.style.display = 'none';
        if (removeBtn) removeBtn.style.display = 'inline-block';
      }
      setQuickBriefDisabled(idx, flow.finished);
      loadCredits();
    }
  };

  async function runAllFlows(payload) {
    try {
      const res = await fetch('/api/run_flows', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: abortController.signal
      });
      if (!res.ok) {
        const err = await res.json();
        alert('ผิดพลาด: ' + (err.error || 'ไม่ทราบสาเหตุ'));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            handleFlowRunSSE(data);
          } catch (e) {}
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') alert('ผิดพลาด: ' + e.message);
    }
  }

  async function runAutoFlow(flow, idx) {
    try {
      const res = await fetch('/api/run_auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          quick_brief: flow.quickBrief || '',
          agents: flow.agents,
          platforms: flow.options.platform,
          media_type: flow.options.media_type,
          media_when: flow.options.media_when,
          auto_image: flow.options.media_when === 'auto' ? flow.options.auto_image : false,
          auto_video: flow.options.media_when === 'auto' ? flow.options.auto_video : false,
          // content_count = จำนวนโพสต์ต่อแพลตฟอร์ม (ไม่ใช่รวมทุกแพลตฟอร์ม)
          // backend จะวนสร้าง platforms ทั้งหมด × content_count โดยเลือกสินค้าครั้งเดียวต่อรอบ
          content_count: flow.options.count,
          product_count: flow.autoCombined ? flow.autoCount : 1,
          combined: flow.autoCombined
        }),
        signal: abortController.signal
      });
      if (!res.ok) {
        const err = await res.json();
        alert('ผิดพลาด: ' + (err.error || 'ไม่ทราบสาเหตุ'));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            data.plan = idx;
            handleFlowRunSSE(data);
          } catch (e) {}
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') alert('ผิดพลาด: ' + e.message);
    }
  }

  function handleFlowRunSSE(data) {
    const planIdx = typeof data.plan === 'number' ? data.plan : 0;
    const flow = flows[planIdx];
    if (!flow) return;

    function resetNextButton() {
      const nextBtn = document.getElementById('flow-next-' + planIdx);
      const backBtn = document.getElementById('flow-back-' + planIdx);
      const removeBtn = document.getElementById('flow-remove-' + planIdx);
      if (nextBtn) { nextBtn.classList.remove('running'); nextBtn.style.display = 'none'; }
      if (backBtn) backBtn.style.display = 'none';
      if (removeBtn) removeBtn.style.display = 'inline-block';
      setQuickBriefDisabled(planIdx, true);
    }

    if (flow.isAuto) {
      const stepEl = document.getElementById('flow-' + planIdx + '-0');
      const statusEl = document.getElementById('flow-' + planIdx + '-0-status');
      const linksEl = document.getElementById('flow-' + planIdx + '-0-links');
      if (data.type === 'selection') {
        if (stepEl) stepEl.className = 'flow-step running';
        let sel = {};
        try { sel = JSON.parse(data.message || '{}'); } catch (e) {}
        const pid = sel.product_id || (sel.product_ids && sel.product_ids.join(' + ')) || '';
        const concept = sel.concept || '';
        if (statusEl) statusEl.textContent = pid ? 'เลือก: ' + pid + (concept ? ' — ' + concept : '') : 'เลือกสินค้า...';
      } else if (data.type === 'agent_start') {
        if (stepEl) stepEl.className = 'flow-step running';
        if (statusEl) statusEl.innerHTML = '<span class="typing">●</span>';
      } else if (data.type === 'agent_done') {
        if (stepEl) stepEl.className = 'flow-step done';
        if (statusEl) statusEl.textContent = '✓';
        if (data.file && linksEl) {
          const setNum = data.set_num || 1;
          const link = document.createElement('span');
          link.className = 'flow-step-link';
          link.textContent = ' 📄' + setNum;
          link.style.cursor = 'pointer';
          link.style.color = '#7c8aff';
          link.onclick = function () { viewResult(data.file); };
          linksEl.appendChild(link);
        }
        _refreshSidebar();
      } else if (data.type === 'error') {
        if (stepEl) stepEl.className = 'flow-step error';
        if (statusEl) statusEl.textContent = '!';
        if (linksEl) linksEl.innerHTML += '<span style="color:#f87171;font-size:12px"> ' + escapeHtml(data.text || data.message || 'ผิดพลาด') + '</span>';
      } else if (data.type === 'status') {
        if (statusEl) statusEl.textContent = data.text || data.message || '';
      } else if (data.type === 'done') {
        flow.finished = true;
        resetNextButton();
        _refreshSidebar();
      }
      return;
    }

    const agentKey = data.agent;
    let stepIdx = -1;
    for (let i = 0; i < flow.agents.length; i++) {
      if (flow.agents[i] === agentKey) { stepIdx = i; break; }
    }
    if (stepIdx === -1 && data.type !== 'done' && data.type !== 'status' && data.type !== 'error') return;

    const stepEl = stepIdx >= 0 ? document.getElementById('flow-' + planIdx + '-' + stepIdx) : null;
    const statusEl = stepIdx >= 0 ? document.getElementById('flow-' + planIdx + '-' + stepIdx + '-status') : null;
    const linksEl = stepIdx >= 0 ? document.getElementById('flow-' + planIdx + '-' + stepIdx + '-links') : null;

    if (data.type === 'agent_start' && stepEl) {
      stepEl.className = 'flow-step running';
      if (statusEl) statusEl.innerHTML = '<span class="typing">●</span>';
    } else if (data.type === 'agent_done' && stepEl) {
      stepEl.className = 'flow-step done';
      if (statusEl) statusEl.textContent = '✓';
      if (data.file && linksEl) {
        const setNum = data.set_num || 1;
        const link = document.createElement('span');
        link.className = 'flow-step-link';
        link.textContent = ' 📄' + setNum;
        link.style.cursor = 'pointer';
        link.style.color = '#7c8aff';
        link.onclick = function () { viewResult(data.file); };
        linksEl.appendChild(link);
      }
      if (data.set_num && data.total_sets && data.set_num < data.total_sets && statusEl) {
        statusEl.textContent = data.set_num + '/' + data.total_sets;
      }
      _refreshSidebar();
    } else if (data.type === 'error' && stepEl) {
      stepEl.className = 'flow-step error';
      if (statusEl) statusEl.textContent = '!';
      if (linksEl) linksEl.innerHTML += '<span style="color:#f87171;font-size:12px"> ' + escapeHtml(data.text || data.message || 'ผิดพลาด') + '</span>';
    } else if (data.type === 'status' && statusEl) {
      statusEl.textContent = data.text || data.message || '';
    } else if (data.type === 'done') {
      flow.finished = true;
      resetNextButton();
      _refreshSidebar();
    }
  }

  function _refreshSidebar() {
    if (typeof loadSessions !== 'function') return;
    if (typeof currentSidebarTab !== 'undefined' && currentSidebarTab !== 'sessions') {
      const tabs = document.querySelectorAll('.sidebar-tab');
      const sessTab = Array.from(tabs).find(t => t.textContent.includes('ผลลัพธ์'));
      if (sessTab) sessTab.click();
      else loadSessions();
    } else {
      loadSessions();
    }
  }

  // --- Helpers ---
  function escapeHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function escapeJs(s) {
    return (s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\n/g, '\\n');
  }

  // Hook sidebar folder loading to re-render step 1 when products are loaded
  const _origLoadFolderList = window.loadFolderList;
  window.loadFolderList = function () {
    const p = _origLoadFolderList.apply(this, arguments);
    if (p && typeof p.then === 'function') {
      p.then(() => {
        refreshProductFolders();
        for (let i = 0; i < flows.length; i++) {
          if ((flowSteps[i] || 1) === 1) renderStep1(i);
        }
      });
    }
    return p;
  };

  // Override stopAll for wizard UI
  window.stopAll = function () {
    if (abortController) { abortController.abort(); abortController = null; }
    fetch('/api/cancel', { method: 'POST' });
    if (typeof AGENT_ORDER !== 'undefined') {
      for (const key of AGENT_ORDER) {
        const box = document.getElementById('box-' + key);
        const status = document.getElementById('status-' + key);
        if (box) box.className = 'agent-box';
        if (status) { status.className = 'agent-status'; status.textContent = 'หยุดการทำงาน'; }
      }
    }
    document.querySelectorAll('.flow-step').forEach(el => { el.className = 'flow-step' + (el.classList.contains('auto') ? ' auto' : ''); });
    document.querySelectorAll('.flow-step-status').forEach(el => { el.textContent = ''; });
    runningAgents = {};
    document.querySelectorAll('[id^="flow-next-"]').forEach(el => {
      const idx = parseInt(el.id.replace('flow-next-', ''));
      const f = flows[idx];
      el.classList.remove('running');
      if (f && f.finished) { el.style.display = 'none'; }
      else { el.textContent = '✓ ยืนยัน'; el.disabled = false; el.style.display = 'inline-block'; }
    });
    document.querySelectorAll('[id^="flow-remove-"]').forEach(el => { el.style.display = 'inline-block'; });
    document.querySelectorAll('[id^="flow-quick-brief-"]').forEach(el => {
      const idx = parseInt(el.id.replace('flow-quick-brief-', ''));
      const f = flows[idx];
      el.disabled = !!(f && f.finished);
    });
    const globalConfirm = document.getElementById('global-confirm');
    const clearBtn = document.getElementById('global-clear');
    const hintEl = document.getElementById('brief-hint');
    if (globalConfirm) { globalConfirm.classList.remove('running'); globalConfirm.textContent = 'ยืนยัน'; globalConfirm.disabled = false; }
    if (clearBtn) clearBtn.disabled = false;
    if (hintEl) hintEl.textContent = 'เลือกสินค้าก่อนกดยืนยัน';
  };

  // Expose helper for schedule modal in web_viewer.py
  window.buildFlowsForBackend = buildFlowsForBackend;

  init();
})();
