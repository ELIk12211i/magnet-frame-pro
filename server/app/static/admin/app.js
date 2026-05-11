/* License Manager — Admin UI client-side
   Vanilla JS, no framework. Loaded at end of <body>. */

(function () {
  'use strict';

  // -- Toast ------------------------------------------------------
  function toast(message, kind) {
    var container = document.getElementById('toastContainer');
    if (!container) return;
    var el = document.createElement('div');
    el.className = 'app-toast ' + (kind || '');
    el.innerHTML = '<i class="bi ' +
      (kind === 'success' ? 'bi-check-circle-fill' : 'bi-info-circle-fill') +
      '"></i><span></span>';
    el.querySelector('span').textContent = message;
    container.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('show'); });
    setTimeout(function () {
      el.classList.remove('show');
      setTimeout(function () { el.remove(); }, 250);
    }, 1800);
  }
  window.adminToast = toast;

  // -- Copy to clipboard ------------------------------------------
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Fallback
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        resolve();
      } catch (e) { reject(e); }
    });
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.copy-btn');
    if (!btn) return;
    ev.preventDefault();
    var text = btn.getAttribute('data-copy-text');
    if (!text) {
      var sel = btn.getAttribute('data-copy-target');
      if (sel) {
        var target = document.querySelector(sel);
        if (target) text = target.innerText || target.textContent;
      }
    }
    if (!text) return;
    copyText(text).then(function () {
      btn.classList.add('copied');
      var label = btn.querySelector('.copy-label');
      var originalLabel = label ? label.textContent : null;
      if (label) label.textContent = 'הועתק!';
      var icon = btn.querySelector('i');
      var originalIcon = icon ? icon.className : null;
      if (icon) icon.className = 'bi bi-check2';
      toast('הועתק ללוח!', 'success');
      setTimeout(function () {
        btn.classList.remove('copied');
        if (label && originalLabel) label.textContent = originalLabel;
        if (icon && originalIcon) icon.className = originalIcon;
      }, 1400);
    }).catch(function () {
      toast('שגיאה בהעתקה');
    });
  });

  // -- Sidebar toggle (mobile) ------------------------------------
  function initSidebar() {
    var toggle = document.getElementById('sidebarToggle');
    var sidebar = document.getElementById('appSidebar');
    var backdrop = document.getElementById('sidebarBackdrop');
    if (!toggle || !sidebar) return;
    function open() {
      sidebar.classList.add('show');
      if (backdrop) backdrop.classList.add('show');
    }
    function close() {
      sidebar.classList.remove('show');
      if (backdrop) backdrop.classList.remove('show');
    }
    toggle.addEventListener('click', function () {
      if (sidebar.classList.contains('show')) close(); else open();
    });
    if (backdrop) backdrop.addEventListener('click', close);
    document.querySelectorAll('.sidebar-link').forEach(function (link) {
      link.addEventListener('click', close);
    });
  }

  // -- Generator form: type + days -----------------------------
  function initGenerator() {
    var form = document.getElementById('generatorForm');
    if (!form) return;

    var typeInputs = form.querySelectorAll('input[name="license_type"]');
    var daysSection = form.querySelector('#daysSection');
    var daysRadios = form.querySelectorAll('input[name="days_preset"]');
    var daysCustomInput = form.querySelector('#daysCustomInput');

    function syncTypeVisibility() {
      var typeVal = null;
      typeInputs.forEach(function (r) { if (r.checked) typeVal = r.value; });
      if (!daysSection) return;
      // yearly -> show days; lifetime & trial -> hide
      if (typeVal === 'yearly') {
        daysSection.style.display = '';
        if (daysCustomInput) daysCustomInput.disabled = false;
      } else {
        daysSection.style.display = 'none';
        if (daysCustomInput) {
          daysCustomInput.value = '';
          daysCustomInput.disabled = true;
        }
      }
    }

    function syncDaysPreset() {
      var preset = null;
      daysRadios.forEach(function (r) { if (r.checked) preset = r.value; });
      if (!daysCustomInput) return;
      if (preset === 'custom') {
        daysCustomInput.style.display = '';
        daysCustomInput.removeAttribute('readonly');
        daysCustomInput.focus();
      } else if (preset) {
        daysCustomInput.style.display = 'none';
        daysCustomInput.value = preset;
      }
    }

    typeInputs.forEach(function (r) { r.addEventListener('change', syncTypeVisibility); });
    daysRadios.forEach(function (r) { r.addEventListener('change', syncDaysPreset); });
    syncTypeVisibility();
    syncDaysPreset();

    // Client-side validation
    form.addEventListener('submit', function (ev) {
      var email = (form.querySelector('[name=customer_email]') || {}).value || '';
      var name = (form.querySelector('[name=customer_name]') || {}).value || '';
      if (email && email.trim() && !name.trim()) {
        ev.preventDefault();
        toast('יש להזין שם לקוח כשנמסר אימייל');
        return false;
      }
      var typeVal = null;
      typeInputs.forEach(function (r) { if (r.checked) typeVal = r.value; });
      if (typeVal === 'yearly' && daysCustomInput) {
        var v = parseInt(daysCustomInput.value || '0', 10);
        if (!v || v < 1) {
          ev.preventDefault();
          toast('יש לבחור מספר ימים חוקי');
          return false;
        }
      }
    });

    var resetBtn = document.getElementById('resetFormBtn');
    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        setTimeout(function () { syncTypeVisibility(); syncDaysPreset(); }, 0);
      });
    }
  }

  // -- Extend modal: radio sync ----------------------------------
  function initExtendModal() {
    var form = document.getElementById('extendForm');
    if (!form) return;
    var radios = form.querySelectorAll('input[name="days_preset"]');
    var input = form.querySelector('#extendDaysInput');
    if (!input) return;
    radios.forEach(function (r) {
      r.addEventListener('change', function () {
        if (r.value === 'custom') {
          input.removeAttribute('readonly');
          input.focus();
        } else {
          input.value = r.value;
        }
      });
    });
  }

  // -- Local-time hydration --------------------------------------
  // Converts every <time class="js-local-dt" datetime="…UTC…"> element
  // to the browser's local time (with seconds). Runs on load and is
  // safe to re-run whenever new rows are injected.
  function _pad(n) { return String(n).padStart(2, '0'); }

  function hydrateLocalTimes(root) {
    var scope = root || document;
    scope.querySelectorAll('time.js-local-dt[datetime]').forEach(function (el) {
      try {
        var d = new Date(el.getAttribute('datetime'));
        if (isNaN(d.getTime())) return;
        el.textContent =
          d.getFullYear() + '-' + _pad(d.getMonth() + 1) + '-' + _pad(d.getDate()) +
          ' ' +
          _pad(d.getHours()) + ':' + _pad(d.getMinutes()) + ':' + _pad(d.getSeconds());
        el.setAttribute('title', d.toString());
      } catch (_) { /* keep server-rendered fallback */ }
    });
    scope.querySelectorAll('time.js-local-date[datetime]').forEach(function (el) {
      try {
        var d = new Date(el.getAttribute('datetime'));
        if (isNaN(d.getTime())) return;
        el.textContent =
          d.getFullYear() + '-' + _pad(d.getMonth() + 1) + '-' + _pad(d.getDate());
        el.setAttribute('title', d.toString());
      } catch (_) { /* keep server-rendered fallback */ }
    });
  }
  window.hydrateLocalTimes = hydrateLocalTimes;

  // -- Sidebar live clock ----------------------------------------
  // Updates every second with the browser's local "HH:MM:SS".
  function initSidebarClock() {
    var timeEl = document.getElementById('sidebarClockTime');
    var dateEl = document.getElementById('sidebarClockDate');
    if (!timeEl) return;

    // Hebrew weekday names so the date line reads naturally RTL.
    var WEEKDAYS_HE = ['ראשון','שני','שלישי','רביעי','חמישי','שישי','שבת'];
    var MONTHS_HE = [
      'ינואר','פברואר','מרץ','אפריל','מאי','יוני',
      'יולי','אוגוסט','ספטמבר','אוקטובר','נובמבר','דצמבר'
    ];

    function tick() {
      var d = new Date();
      timeEl.textContent =
        _pad(d.getHours()) + ':' +
        _pad(d.getMinutes()) + ':' +
        _pad(d.getSeconds());
      if (dateEl) {
        dateEl.textContent =
          'יום ' + WEEKDAYS_HE[d.getDay()] + ' · ' +
          d.getDate() + ' ב' + MONTHS_HE[d.getMonth()] + ' ' + d.getFullYear();
      }
    }

    tick();
    // Align the first tick to the next whole second so the seconds
    // counter looks smooth even on slow page loads.
    var delay = 1000 - (new Date().getMilliseconds());
    setTimeout(function () {
      tick();
      setInterval(tick, 1000);
    }, delay);
  }

  // -- Init ------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    initSidebar();
    initGenerator();
    initExtendModal();
    hydrateLocalTimes();
    initSidebarClock();
  });
})();
