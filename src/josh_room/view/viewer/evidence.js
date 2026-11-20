// Inspector & Evidence Viewer: Details, Timeline, Lazy PDF.js, Image Comparison, Inert HTML Report.
(function (global) {
  'use strict';

  const MAX_CANVAS_DIM = 2048;

  class EvidenceDrawer {
    constructor(dataLayer, loadPdfJsFn) {
      this.data = dataLayer;
      this.loadPdfJs = loadPdfJsFn;
      this.drawerEl = document.getElementById('inspector-drawer');
      this.titleEl = document.getElementById('inspector-title');
      this.pathEl = document.getElementById('inspector-path');
      this.badgeEl = document.getElementById('inspector-kind-badge');
      this.countBadgeEl = document.getElementById('evidence-count-badge');
      this.tabDetailsEl = document.getElementById('tab-details');
      this.tabEvidenceEl = document.getElementById('tab-evidence');
      this.tabTimelineEl = document.getElementById('tab-timeline');
      this.lightboxEl = document.getElementById('lightbox-modal');
      this.lightboxImg = document.getElementById('lightbox-img');
      this.lightboxCaption = document.getElementById('lightbox-caption');

      this.currentItem = null;
      this.activeTab = 'details';
      this._attRequestSeq = 0;
      this._pdfObserver = null;
      this._pdfDoc = null;
      this._pdfScale = 1.0;
      this._pdfFitMode = 'width';
      this._pdfPagesRendered = 0;

      this._bindTabs();
      this._bindLightbox();
    }

    _bindTabs() {
      this.drawerEl.querySelectorAll('.tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          this.switchTab(btn.getAttribute('data-tab') || 'details');
        });
      });
    }

    _bindLightbox() {
      const closeBtn = document.getElementById('btn-lightbox-close');
      if (closeBtn) {
        closeBtn.addEventListener('click', () => { this.lightboxEl.hidden = true; });
      }
      this.lightboxEl.addEventListener('click', (ev) => {
        if (ev.target === this.lightboxEl) this.lightboxEl.hidden = true;
      });
    }

    switchTab(tabName) {
      this.activeTab = tabName;
      this.drawerEl.querySelectorAll('.tab-btn').forEach((btn) => {
        const active = btn.getAttribute('data-tab') === tabName;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      this.tabDetailsEl.hidden = tabName !== 'details';
      this.tabEvidenceEl.hidden = tabName !== 'evidence';
      this.tabTimelineEl.hidden = tabName !== 'timeline';
    }

    hide() {
      this._teardownPdf();
      this.currentItem = null;
      this.drawerEl.hidden = true;
    }

    isOpen() {
      return !this.drawerEl.hidden;
    }

    showItem(item, descriptor, state, quantileBin) {
      this._teardownPdf();
      this.currentItem = item;
      this.drawerEl.hidden = false;

      const cue = item.kind === 'fail' ? '× FAIL' : (item.kind === 'skip' ? '○ SKIP' : '● PASS');
      this.badgeEl.textContent = cue;
      this.badgeEl.className = `kind-badge kind-${item.kind}`;
      this.titleEl.textContent = item.label;
      this.pathEl.textContent = `/${item.group}/${item.stage}/${item.id}`;
      const atts = item.attachments || [];
      this.countBadgeEl.textContent = String(atts.length);

      this._renderDetails(item, descriptor, quantileBin);
      this._renderTimeline(item, state);
      this._renderEvidence(item);
      this.switchTab('details');
    }

    _renderDetails(item, descriptor, quantileBin) {
      const unit = descriptor.metric && descriptor.metric.unit ? ` ${descriptor.metric.unit}` : '';
      const metricName = descriptor.metric ? descriptor.metric.name : 'metric';
      const rows = [
        ['ID', item.id],
        [descriptor.group_label || 'Group', item.group],
        ['Stage', item.stage],
        ['Outcome', item.kind],
        [metricName, item.metric != null ? `${item.metric}${unit}${quantileBin ? ` (Bin ${quantileBin}/5)` : ''}` : '—'],
        ['Updated', item.updated_at],
      ];
      for (const [k, v] of Object.entries(item.fields || {})) {
        rows.push([k, v === null ? 'null' : String(v)]);
      }

      const grid = document.createElement('div');
      grid.className = 'kv-grid';
      for (const [k, v] of rows) {
        const dk = document.createElement('div');
        dk.className = 'kv-key';
        dk.textContent = k;
        const dv = document.createElement('div');
        dv.className = 'kv-val';
        dv.textContent = v;
        grid.append(dk, dv);
      }

      this.tabDetailsEl.textContent = '';
      this.tabDetailsEl.appendChild(grid);

      if ((item.attachments || []).length > 0) {
        const hintBtn = document.createElement('button');
        hintBtn.type = 'button';
        hintBtn.className = 'hud-btn hud-btn-accent';
        hintBtn.textContent = `Open ${item.attachments.length} attached evidence file${item.attachments.length > 1 ? 's' : ''}`;
        hintBtn.addEventListener('click', () => this.switchTab('evidence'));
        this.tabDetailsEl.appendChild(hintBtn);
      }
    }

    _renderTimeline(item, state) {
      const steps = (state.steps || []).filter((s) => s.item_id === item.id);
      this.tabTimelineEl.textContent = '';
      if (!steps.length) {
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = 'No lineage steps recorded for this item.';
        this.tabTimelineEl.appendChild(p);
        return;
      }
      const ul = document.createElement('ul');
      ul.className = 'timeline-list';
      for (const step of steps) {
        const li = document.createElement('li');
        const strong = document.createElement('strong');
        strong.textContent = step.name;
        const ts = document.createElement('div');
        ts.className = 'muted-text';
        ts.style.fontFamily = 'var(--font-mono)';
        ts.textContent = step.at;
        li.append(strong, ts);
        ul.appendChild(li);
      }
      this.tabTimelineEl.appendChild(ul);
    }

    _renderEvidence(item) {
      this.tabEvidenceEl.textContent = '';
      const atts = item.attachments || [];
      if (!atts.length) {
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = 'No evidence attached to this item.';
        this.tabEvidenceEl.appendChild(p);
        return;
      }

      const listEl = document.createElement('div');
      listEl.className = 'att-list';
      const stageEl = document.createElement('div');
      stageEl.className = 'att-stage';
      this.tabEvidenceEl.append(listEl, stageEl);

      const orderScore = (mt) => (mt === 'application/pdf' ? 0 : (mt.startsWith('image/') ? 1 : (mt === 'text/html' ? 2 : 3)));
      const sortedAtts = atts.slice().sort((a, b) => orderScore(a.media_type) - orderScore(b.media_type) || a.name.localeCompare(b.name));
      // Group image attachments together if >=2 images exist for side-by-side comparison
      const images = sortedAtts.filter((a) => a.media_type.startsWith('image/'));
      const buttons = [];

      const selectAtt = (att, btn) => {
        buttons.forEach((b) => b.classList.remove('active'));
        if (btn) btn.classList.add('active');
        this._attRequestSeq += 1;
        const reqSeq = this._attRequestSeq;
        this._teardownPdf();
        stageEl.textContent = '';
        if (att.media_type === 'application/pdf') {
          this._openPdf(item.id, att, stageEl, reqSeq);
        } else if (att.media_type.startsWith('image/')) {
          this._openImages(item.id, images.length >= 2 ? images : [att], stageEl, reqSeq);
        } else if (att.media_type === 'text/html') {
          this._openInertHtml(item.id, att, stageEl, reqSeq);
        } else {
          const p = document.createElement('p');
          p.className = 'muted-text';
          p.textContent = `${att.name} (${att.media_type}, ${att.size} bytes) — listed only, not embedded.`;
          stageEl.appendChild(p);
        }
      };

      sortedAtts.forEach((att, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'att-pill';
        btn.setAttribute('data-attachment-name', att.name);
        btn.textContent = `${att.name} (${Math.max(1, Math.round(att.size / 1024))} KB)`;
        btn.addEventListener('click', () => selectAtt(att, btn));
        buttons.push(btn);
        listEl.appendChild(btn);
        if (idx === 0) selectAtt(att, btn);
      });
    }

    _teardownPdf() {
      if (this._pdfObserver) {
        this._pdfObserver.disconnect();
        this._pdfObserver = null;
      }
      if (this._pdfDoc) {
        try { this._pdfDoc.destroy(); } catch (_) {}
        this._pdfDoc = null;
      }
    }

    async _openPdf(itemId, att, container, reqSeq) {
      const startMs = performance.now();
      const status = document.createElement('div');
      status.className = 'muted-text';
      status.textContent = `Loading ${att.name}…`;
      container.appendChild(status);

      try {
        const [pdfjsLib, payload] = await Promise.all([
          this.loadPdfJs(),
          this.data.loadAttachmentBytes(itemId, att.name),
        ]);
        if (reqSeq !== this._attRequestSeq) return;
        if (!payload || !payload.bytes) {
          status.textContent = 'PDF payload unavailable (snapshot exported with --no-attachments).';
          return;
        }
        const loadingTask = pdfjsLib.getDocument({
          data: payload.bytes,
          useSystemFonts: true,
          isEvalSupported: false,
        });
        const pdfDoc = await loadingTask.promise;
        if (reqSeq !== this._attRequestSeq) {
          try { pdfDoc.destroy(); } catch (_) {}
          return;
        }
        this._pdfDoc = pdfDoc;
        this._pdfFitMode = 'width';
        this._pdfScale = 1.0;

        container.textContent = '';

        // Controls: Fit Width / Fit Page / Zoom -/+ / Page indicator
        const controls = document.createElement('div');
        controls.className = 'pdf-controls';
        const btnFitW = document.createElement('button');
        btnFitW.type = 'button';
        btnFitW.className = 'hud-btn hud-sm';
        btnFitW.textContent = 'Fit Width';
        const btnFitP = document.createElement('button');
        btnFitP.type = 'button';
        btnFitP.className = 'hud-btn hud-sm';
        btnFitP.textContent = 'Fit Page';
        const btnZoomOut = document.createElement('button');
        btnZoomOut.type = 'button';
        btnZoomOut.className = 'hud-btn hud-sm';
        btnZoomOut.textContent = '−';
        const btnZoomIn = document.createElement('button');
        btnZoomIn.type = 'button';
        btnZoomIn.className = 'hud-btn hud-sm';
        btnZoomIn.textContent = '+';
        const info = document.createElement('span');
        info.className = 'muted-text';
        info.style.marginLeft = 'auto';
        info.textContent = `${pdfDoc.numPages} pages`;
        controls.append(btnFitW, btnFitP, btnZoomOut, btnZoomIn, info);

        const viewportEl = document.createElement('div');
        viewportEl.className = 'pdf-viewport';
        viewportEl.tabIndex = 0;
        viewportEl.setAttribute('aria-label', `PDF document ${att.name}`);
        container.append(controls, viewportEl);

        const slots = [];
        for (let p = 1; p <= pdfDoc.numPages; p += 1) {
          const slot = document.createElement('div');
          slot.className = 'pdf-page-slot';
          slot.dataset.pageNum = String(p);
          slot.style.minHeight = '320px';
          const lbl = document.createElement('div');
          lbl.className = 'pdf-page-label';
          lbl.textContent = `Page ${p} / ${pdfDoc.numPages}`;
          slot.appendChild(lbl);
          viewportEl.appendChild(slot);
          slots.push(slot);
        }

        const renderPageIntoSlot = async (slot) => {
          const pageNum = Number(slot.dataset.pageNum);
          if (slot.dataset.rendered === '1' || !this._pdfDoc) return;
          slot.dataset.rendered = '1';
          const page = await pdfDoc.getPage(pageNum);
          const unscaled = page.getViewport({ scale: 1.0 });
          const availW = Math.max(220, viewportEl.clientWidth - 28);
          const availH = Math.max(260, viewportEl.clientHeight - 28);
          const baseScale = this._pdfFitMode === 'page'
            ? Math.min(availW / unscaled.width, availH / unscaled.height)
            : (availW / unscaled.width);
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          let targetScale = baseScale * this._pdfScale;
          // Enforce per-canvas pixel cap
          const maxScaleByDim = Math.min(
            MAX_CANVAS_DIM / (unscaled.width * dpr),
            MAX_CANVAS_DIM / (unscaled.height * dpr)
          );
          targetScale = Math.min(targetScale, maxScaleByDim);

          const vp = page.getViewport({ scale: targetScale * dpr });
          const canvas = document.createElement('canvas');
          canvas.width = Math.max(1, Math.floor(vp.width));
          canvas.height = Math.max(1, Math.floor(vp.height));
          canvas.style.width = `${Math.round(vp.width / dpr)}px`;
          canvas.style.height = `${Math.round(vp.height / dpr)}px`;
          const ctx = canvas.getContext('2d');
          await page.render({ canvasContext: ctx, viewport: vp }).promise;
          if (slot.dataset.rendered !== '1') return;
          slot.insertBefore(canvas, slot.firstChild);
          slot.style.minHeight = `${Math.round(vp.height / dpr) + 24}px`;
          this._pdfPagesRendered += 1;
          if (pageNum === 1) {
            const ms = Math.round((performance.now() - startMs) * 10) / 10;
            info.textContent = `${pdfDoc.numPages} pages · p1 in ${ms}ms`;
            info.dataset.firstPageMs = String(ms);
          }
        };

        const releaseOffscreenSlot = (slot) => {
          const canvas = slot.querySelector('canvas');
          if (canvas) {
            canvas.width = 0;
            canvas.height = 0;
            canvas.remove();
          }
          slot.dataset.rendered = '0';
        };

        const rerenderVisible = () => {
          const ratioY = viewportEl.scrollHeight > 0 ? viewportEl.scrollTop / viewportEl.scrollHeight : 0;
          slots.forEach(releaseOffscreenSlot);
          if (this._pdfObserver) {
            this._pdfObserver.disconnect();
            slots.forEach((s) => this._pdfObserver.observe(s));
          }
          requestAnimationFrame(() => {
            viewportEl.scrollTop = Math.round(ratioY * viewportEl.scrollHeight);
          });
        };

        this._pdfObserver = new IntersectionObserver(
          (entries) => {
            for (const entry of entries) {
              if (entry.isIntersecting) {
                renderPageIntoSlot(entry.target);
              } else {
                releaseOffscreenSlot(entry.target);
              }
            }
          },
          { root: viewportEl, rootMargin: '120px 0px' }
        );
        slots.forEach((s) => this._pdfObserver.observe(s));
        // Eagerly render page 1 for <500ms first-page visibility budget
        await renderPageIntoSlot(slots[0]);

        btnFitW.addEventListener('click', () => { this._pdfFitMode = 'width'; this._pdfScale = 1.0; rerenderVisible(); });
        btnFitP.addEventListener('click', () => { this._pdfFitMode = 'page'; this._pdfScale = 1.0; rerenderVisible(); });
        btnZoomIn.addEventListener('click', () => { this._pdfScale = Math.min(3.0, this._pdfScale * 1.2); rerenderVisible(); });
        btnZoomOut.addEventListener('click', () => { this._pdfScale = Math.max(0.5, this._pdfScale / 1.2); rerenderVisible(); });

        // Ctrl-wheel zoom preserving scroll anchor + drag to pan + keyboard paging
        viewportEl.addEventListener('wheel', (ev) => {
          if (!ev.ctrlKey) return;
          ev.preventDefault();
          this._pdfScale = ev.deltaY < 0
            ? Math.min(3.0, this._pdfScale * 1.15)
            : Math.max(0.5, this._pdfScale / 1.15);
          rerenderVisible();
        }, { passive: false });

        let panActive = false;
        let panX = 0;
        let panY = 0;
        viewportEl.addEventListener('pointerdown', (ev) => {
          panActive = true;
          panX = ev.clientX;
          panY = ev.clientY;
        });
        viewportEl.addEventListener('pointermove', (ev) => {
          if (!panActive) return;
          viewportEl.scrollLeft -= ev.clientX - panX;
          viewportEl.scrollTop -= ev.clientY - panY;
          panX = ev.clientX;
          panY = ev.clientY;
        });
        window.addEventListener('pointerup', () => { panActive = false; });

        viewportEl.addEventListener('keydown', (ev) => {
          if (ev.key === 'PageDown') { ev.preventDefault(); viewportEl.scrollBy(0, viewportEl.clientHeight * 0.85); }
          else if (ev.key === 'PageUp') { ev.preventDefault(); viewportEl.scrollBy(0, -viewportEl.clientHeight * 0.85); }
        });
      } catch (err) {
        status.textContent = `Could not render PDF: ${err && err.message ? err.message : 'unknown error'}`;
      }
    }

    async _openImages(itemId, imageAtts, container, reqSeq) {
      const grid = document.createElement('div');
      grid.className = 'img-compare-grid';
      container.appendChild(grid);

      for (const att of imageAtts) {
        const entry = await this.data.getAttachmentBlobUrl(itemId, att.name);
        if (reqSeq !== this._attRequestSeq) return;
        const card = document.createElement('div');
        card.className = 'img-card';
        const caption = document.createElement('div');
        caption.className = 'muted-text';
        caption.style.marginBottom = '6px';
        caption.textContent = att.name;
        card.appendChild(caption);

        if (!entry) {
          const msg = document.createElement('div');
          msg.className = 'muted-text';
          msg.textContent = 'Image payload unavailable';
          card.appendChild(msg);
        } else {
          const img = document.createElement('img');
          img.src = entry.url;
          img.alt = att.name;
          img.addEventListener('load', () => {
            // Never upscale past 1.25x natural size
            img.style.maxWidth = `${Math.round(img.naturalWidth * 1.25)}px`;
          });
          img.addEventListener('click', () => {
            this.lightboxCaption.textContent = `${itemId} / ${att.name}`;
            this.lightboxImg.src = entry.url;
            this.lightboxImg.onload = () => {
              this.lightboxImg.style.maxWidth = `${Math.round(this.lightboxImg.naturalWidth * 1.25)}px`;
              this.lightboxImg.style.maxHeight = `${Math.round(this.lightboxImg.naturalHeight * 1.25)}px`;
            };
            this.lightboxEl.hidden = false;
          });
          card.appendChild(img);
        }
        grid.appendChild(card);
      }
    }

    async _openInertHtml(itemId, att, container, reqSeq) {
      // Security requirement: HTML attachments are NEVER executed!
      // Parsed inertly with DOMParser; scripts/iframes/handlers are stripped and rendered as safe DOM.
      const payload = await this.data.loadAttachmentBytes(itemId, att.name);
      if (reqSeq !== this._attRequestSeq) return;
      if (!payload || !payload.bytes) {
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = 'HTML report payload unavailable.';
        container.appendChild(p);
        return;
      }
      const rawHtml = new TextDecoder('utf-8').decode(payload.bytes);
      const doc = new DOMParser().parseFromString(rawHtml, 'text/html');
      const strippedScripts = doc.querySelectorAll('script, iframe, object, embed').length;

      const wrap = document.createElement('div');
      wrap.className = 'inert-report';

      const badge = document.createElement('div');
      badge.className = 'inert-badge';
      badge.textContent = `INERT PARSED REPORT (scripts blocked: ${strippedScripts})`;
      wrap.appendChild(badge);

      const headings = doc.querySelectorAll('h1, h2, h3');
      if (headings.length) {
        const h = document.createElement('h3');
        h.style.margin = '4px 0 8px';
        h.textContent = headings[0].textContent.trim();
        wrap.appendChild(h);
      }

      doc.querySelectorAll('p').forEach((pEl) => {
        const text = pEl.textContent.trim();
        if (!text) return;
        const p = document.createElement('p');
        p.className = 'muted-text';
        p.textContent = text;
        wrap.appendChild(p);
      });

      doc.querySelectorAll('table').forEach((srcTable) => {
        const table = document.createElement('table');
        table.className = 'inert-table';
        srcTable.querySelectorAll('tr').forEach((srcRow, rowIdx) => {
          const tr = document.createElement('tr');
          const cells = srcRow.querySelectorAll('th, td');
          cells.forEach((srcCell) => {
            const cell = document.createElement(srcCell.tagName.toLowerCase() === 'th' || rowIdx === 0 ? 'th' : 'td');
            cell.textContent = srcCell.textContent.trim();
            tr.appendChild(cell);
          });
          table.appendChild(tr);
        });
        wrap.appendChild(table);
      });

      container.appendChild(wrap);
    }
  }

  global.RoomViewEvidence = { EvidenceDrawer };
})(window);
