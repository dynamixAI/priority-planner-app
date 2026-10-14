// Agi's guided tour, used on the dashboard and Settings.
// AgiTour.start(steps, options) highlights one part of the page at a time.
// A step is { title, text, target, optional, counted, demo, finish, enter,
// leave }. target returns an element, or a [first, last] pair to highlight
// both and what's between. enter(ctx) can act things out on the page with a
// pointer hand; leave() puts the page back.
(function () {
  const tour = { steps: [], i: 0, spot: null, card: null, blocker: null,
                 agi: null, hand: null, opts: {}, run: 0, entered: null };

  // Agi, the Agenndar guide: a little planner page with binder rings.
  function agiSvg(extraClass) {
    return '<svg class="agi ' + (extraClass || '') + '" viewBox="0 0 120 140"'
      + ' aria-hidden="true" focusable="false">'
      + '<ellipse class="agi-shadow" cx="60" cy="134" rx="26" ry="4"/>'
      + '<g class="agi-bob">'
      + '<ellipse class="agi-foot" cx="45" cy="121" rx="10" ry="6"/>'
      + '<ellipse class="agi-foot" cx="75" cy="121" rx="10" ry="6"/>'
      + '<g class="agi-arm-l"><path class="agi-limb" d="M24 76 Q10 82 9 96"/>'
      + '<circle class="agi-hand" cx="9" cy="97" r="6"/></g>'
      + '<rect class="agi-body" x="20" y="28" width="80" height="92" rx="24"/>'
      + '<path class="agi-band" d="M20 52 V50 A22 22 0 0 1 42 28 H78 A22 22 0'
      + ' 0 1 100 50 V52 Z"/>'
      + '<rect class="agi-ring" x="36" y="16" width="9" height="22" rx="4.5"/>'
      + '<rect class="agi-ring" x="75" y="16" width="9" height="22" rx="4.5"/>'
      + '<rect class="agi-face" x="30" y="58" width="60" height="50" rx="15"/>'
      + '<g class="agi-eyes">'
      + '<ellipse class="agi-ink" cx="48" cy="78" rx="5.5" ry="7"/>'
      + '<ellipse class="agi-ink" cx="72" cy="78" rx="5.5" ry="7"/>'
      + '<circle class="agi-glint" cx="50" cy="75" r="2"/>'
      + '<circle class="agi-glint" cx="74" cy="75" r="2"/></g>'
      + '<ellipse class="agi-cheek" cx="39" cy="90" rx="5" ry="3"/>'
      + '<ellipse class="agi-cheek" cx="81" cy="90" rx="5" ry="3"/>'
      + '<path class="agi-smile" d="M52 91 Q60 98 68 91"/>'
      + '<g class="agi-arm-r"><path class="agi-limb" d="M98 76 H118"/>'
      + '<circle class="agi-hand" cx="119" cy="76" r="6"/></g>'
      + '</g></svg>';
  }

  function first(el) { return Array.isArray(el) ? el[0] : el; }

  function rectOf(el) {
    if (!Array.isArray(el)) return el.getBoundingClientRect();
    const a = el[0].getBoundingClientRect();
    const b = el[el.length - 1].getBoundingClientRect();
    const top = Math.min(a.top, b.top), bottom = Math.max(a.bottom, b.bottom);
    const left = Math.min(a.left, b.left), right = Math.max(a.right, b.right);
    return { top, bottom, left, right, width: right - left, height: bottom - top };
  }

  function visible(el) {
    if (Array.isArray(el)) return el.length > 0 && el.every(visible);
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0
      && getComputedStyle(el).visibility !== 'hidden';
  }

  // Agi stands beside the tour card and points at what's highlighted.
  function placeAgi(el, cl, ct, cw, ch) {
    const a = tour.agi;
    if (!a) return;
    const W = 84, H = 98, s = W / 120;
    const svg = a.querySelector('.agi');
    let mx, my;
    if (!el) {
      mx = cl + cw / 2 - W / 2;
      my = ct - H + 12;
    } else if (cl - W - 8 >= 4) {
      mx = cl - W - 8;
      my = ct + 6;
    } else if (cl + cw + W + 8 <= innerWidth - 4) {
      mx = cl + cw + 8;
      my = ct + 6;
    } else {
      // No room at the sides (phones): stand on the card's far side from
      // the highlight, so Agi never covers what it's pointing at.
      const tr = rectOf(el);
      const targetAbove = tr.top + tr.height / 2 < ct;
      mx = cl + 14;
      if (targetAbove && ct + ch + H + 4 <= innerHeight - 4) {
        my = ct + ch + 4;
      } else if (targetAbove) {
        mx = cl + cw - W - 6;
        my = ct - H + 34;
      } else {
        my = ct - H + 12;
      }
    }
    mx = Math.max(4, Math.min(innerWidth - W - 4, mx));
    my = Math.max(4, Math.min(innerHeight - H - 4, my));
    a.style.left = mx + 'px';
    a.style.top = my + 'px';
    svg.classList.toggle('waving', !el);
    svg.classList.toggle('pointing', !!el);
    if (!el) {
      a.classList.remove('flip');
      return;
    }
    // Aim at the nearest edge of the highlight, or its middle if Agi is on it.
    const r = rectOf(el);
    const cx = mx + W / 2, cy = my + H / 2;
    let tx = Math.max(r.left, Math.min(cx, r.right));
    let ty = Math.max(r.top, Math.min(cy, r.bottom));
    if (tx === cx && ty === cy) {
      tx = r.left + r.width / 2;
      ty = r.top + r.height / 2;
    }
    const flip = tx < cx - 4;
    a.classList.toggle('flip', flip);
    const sx = mx + (flip ? 22 : 98) * s, sy = my + 76 * s;
    let ang = Math.atan2(ty - sy, tx - sx) * 180 / Math.PI;
    if (flip) ang = 180 - ang;
    if (ang > 180) ang -= 360;
    ang = Math.max(-110, Math.min(110, ang));
    svg.style.setProperty('--agi-aim', ang + 'deg');
  }

  function demoHtml(kind) {
    if (kind === 'drag') {
      return '<div class="tour-demo"><div class="demo-cols">'
        + '<span></span><span></span><span></span></div>'
        + '<div class="demo-card"></div>'
        + '<div class="demo-hand">\u{1F446}</div></div>';
    }
    if (kind === 'checkin') {
      return '<div class="tour-demo"><div class="demo-buttons"><b>Done</b>'
        + '<b>Done late</b><b>Push</b><b>Missed</b></div></div>';
    }
    if (kind === 'score') {
      return '<div class="tour-demo"><div class="demo-meter"><i></i></div>'
        + '<div class="demo-legend">Do what you planned and it climbs</div></div>';
    }
    if (kind === 'meeting') {
      return '<div class="tour-demo"><div class="demo-switch"><i></i>'
        + '<span>Task</span><span>Meeting</span></div>'
        + '<div class="demo-entry"><b>Write report</b><b>\u{1F4DE} Call with Ada</b>'
        + '</div></div>';
    }
    if (kind === 'slots') {
      return '<div class="tour-demo"><div class="demo-buttons slots">'
        + '<b>09:00</b><b>10:30</b><b>13:00</b><b>15:30</b></div>'
        + '<div class="demo-legend">They pick a free time, you get notified</div>'
        + '</div>';
    }
    if (kind === 'rows') {
      return '<div class="tour-demo"><div class="demo-rows">'
        + '<span></span><span></span><span></span></div></div>';
    }
    return '';
  }

  // A pointer hand that moves around the page like someone using it.
  function handAt(x, y) {
    const h = tour.hand;
    h.style.left = x + 'px';
    h.style.top = y + 'px';
  }

  function centreOf(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function makeCtx(run) {
    const alive = () => tour.run === run && !!tour.card;
    const ctx = {
      alive,
      wait: ms => new Promise(res => setTimeout(() => res(alive()), ms)),
      place: () => place(),
      // Move the hand to an element or a {x, y} point.
      async moveTo(to, ms) {
        if (!alive()) return false;
        const p = to && to.getBoundingClientRect ? centreOf(to) : to;
        const h = tour.hand;
        if (!h.classList.contains('on')) {
          h.style.transition = 'none';
          handAt(p.x + 40, p.y + 60);
          void h.offsetWidth;
          h.classList.add('on');
        }
        h.style.transition = 'left ' + (ms || 600) + 'ms cubic-bezier(.4,0,.2,1), '
          + 'top ' + (ms || 600) + 'ms cubic-bezier(.4,0,.2,1), opacity 0.2s';
        handAt(p.x, p.y);
        return ctx.wait((ms || 600) + 40);
      },
      async tap() {
        if (!alive()) return false;
        tour.hand.classList.remove('tap');
        void tour.hand.offsetWidth;
        tour.hand.classList.add('tap');
        return ctx.wait(260);
      },
      press(down) { if (alive()) tour.hand.classList.toggle('down', !!down); },
      hideHand() { if (tour.hand) tour.hand.classList.remove('on', 'down'); }
    };
    return ctx;
  }

  function leaveStep() {
    tour.run++;
    if (tour.hand) tour.hand.classList.remove('on', 'down', 'tap');
    const s = tour.entered;
    tour.entered = null;
    if (s && s.leave) {
      try { s.leave(); } catch (e) {}
    }
  }

  function enterStep(step) {
    if (!step.enter) return;
    tour.entered = step;
    try {
      const r = step.enter(makeCtx(tour.run));
      if (r && r.catch) r.catch(() => {});
    } catch (e) {}
  }

  function targetOf(step) {
    const el = step && step.target ? step.target() : null;
    return visible(el) ? el : null;
  }

  function showStep(i) {
    leaveStep();
    // Skip steps whose part of the page isn't showing.
    const dir = i >= tour.i ? 1 : -1;
    while (i > 0 && i < tour.steps.length - 1 && tour.steps[i].target
           && !tour.steps[i].optional && !targetOf(tour.steps[i])) {
      i += dir;
    }
    tour.i = i;
    const step = tour.steps[i];
    const shown = tour.steps.filter((s, k) =>
      !s.target || s.optional || s.counted || k === i || targetOf(s));
    const pos = shown.indexOf(step);
    const last = i === tour.steps.length - 1;
    const o = tour.opts;
    tour.card.innerHTML =
      '<div class="tour-step">' + (pos + 1) + ' of ' + shown.length + '</div>'
      + '<h3>' + step.title + '</h3><p>' + step.text + '</p>' + demoHtml(step.demo)
      + '<div class="tour-actions"><div class="tour-dots">'
      + (last && o.altLabel ? '' : shown.map((s, k) =>
        '<i class="' + (k === pos ? 'on' : '') + '"></i>').join(''))
      + '</div>'
      + (last ? '' : '<button class="tour-btn link" data-t="skip">Skip</button>')
      + (last && o.altLabel
        ? '<button class="tour-btn" data-t="alt">' + o.altLabel + '</button>' : '')
      + (i > 0 && !last
        ? '<button class="tour-btn" data-t="back">Back</button>' : '')
      + '<button class="tour-btn primary" data-t="next">'
      + (i === 0 ? (o.startLabel || 'Show me')
        : last ? (o.finishLabel || 'Done') : 'Next')
      + '</button></div>';
    tour.card.querySelectorAll('[data-t]').forEach(b => {
      b.onclick = () => action(b.dataset.t);
    });
    const demo = tour.card.querySelector('.tour-demo');
    if (demo) demo.style.setProperty('--demo-w', demo.offsetWidth + 'px');
    tour.card.classList.remove('enter');
    void tour.card.offsetWidth;
    tour.card.classList.add('enter');

    const el = targetOf(step);
    if (el) {
      const r = rectOf(el);
      if (r.top < 70 || r.bottom > innerHeight - 70) {
        first(el).scrollIntoView({
          block: r.height > innerHeight * 0.6 ? 'start' : 'center',
          behavior: 'smooth'
        });
      }
    }
    enterStep(step);
    place();
    setTimeout(place, 400);
    tour.card.querySelector('.tour-btn.primary').focus({ preventScroll: true });
  }

  function place() {
    if (!tour.card) return;
    const el = targetOf(tour.steps[tour.i]);
    const pad = 8, gap = 14;
    const cw = tour.card.offsetWidth, ch = tour.card.offsetHeight;
    if (!el) {
      tour.spot.classList.add('centered');
      Object.assign(tour.spot.style, {
        top: innerHeight / 2 + 'px', left: innerWidth / 2 + 'px',
        width: '0px', height: '0px'
      });
      tour.card.style.left = Math.max(16, (innerWidth - cw) / 2) + 'px';
      // Leave room above the card for Agi.
      const ctop = Math.max(110, (innerHeight - ch) / 2);
      tour.card.style.top = ctop + 'px';
      placeAgi(null, Math.max(16, (innerWidth - cw) / 2), ctop, cw, ch);
      return;
    }
    tour.spot.classList.remove('centered');
    const r = rectOf(el);
    const top = Math.max(4, r.top - pad);
    const bottom = Math.min(innerHeight - 4, r.bottom + pad);
    Object.assign(tour.spot.style, {
      top: top + 'px', left: Math.max(4, r.left - pad) + 'px',
      width: Math.min(innerWidth - 8, r.width + pad * 2) + 'px',
      height: Math.max(0, bottom - top) + 'px'
    });
    // Card below the highlight if it fits, else above, else over its bottom.
    let cardTop;
    if (bottom + gap + ch < innerHeight - 8) cardTop = bottom + gap;
    else if (top - gap - ch > 8) cardTop = top - gap - ch;
    else cardTop = innerHeight - ch - 16;
    let cardLeft = r.left + r.width / 2 - cw / 2;
    cardLeft = Math.max(16, Math.min(innerWidth - cw - 16, cardLeft));
    tour.card.style.top = cardTop + 'px';
    tour.card.style.left = cardLeft + 'px';
    placeAgi(el, cardLeft, cardTop, cw, ch);
  }

  function action(what) {
    if (what === 'back') return showStep(Math.max(0, tour.i - 1));
    if (what === 'next' && tour.i < tour.steps.length - 1) {
      return showStep(tour.i + 1);
    }
    const onEnd = tour.opts.onEnd;
    closeParts();
    // 'next' on the last step, 'alt', or 'skip'
    if (onEnd) onEnd(what === 'next' ? 'finish' : what);
  }

  function keys(e) {
    if (!tour.card) return;
    if (e.key === 'Escape') action('skip');
    else if (e.key === 'ArrowRight') action('next');
    else if (e.key === 'ArrowLeft' && tour.i > 0) action('back');
  }

  function closeParts() {
    leaveStep();
    [tour.spot, tour.card, tour.blocker, tour.agi, tour.hand]
      .forEach(n => n && n.remove());
    tour.spot = tour.card = tour.blocker = tour.agi = tour.hand = null;
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    document.removeEventListener('keydown', keys);
  }

  function start(steps, opts) {
    closeParts();
    tour.steps = steps;
    tour.opts = opts || {};
    tour.i = 0;
    tour.blocker = document.createElement('div');
    tour.blocker.className = 'tour-blocker';
    tour.spot = document.createElement('div');
    tour.spot.className = 'tour-spot centered';
    tour.card = document.createElement('div');
    tour.card.className = 'tour-card';
    tour.card.setAttribute('role', 'dialog');
    tour.card.setAttribute('aria-live', 'polite');
    tour.agi = document.createElement('div');
    tour.agi.className = 'tour-agi';
    tour.agi.innerHTML = agiSvg();
    tour.hand = document.createElement('div');
    tour.hand.className = 'tour-hand';
    tour.hand.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<path d="M5 2.5 L5 19 L9.2 15.2 L12 21.5 L14.8 20.3 L12.1 14.1'
      + ' L18 14.1 Z"/></svg><i></i>';
    document.body.append(tour.blocker, tour.spot, tour.card, tour.agi, tour.hand);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    document.addEventListener('keydown', keys);
    showStep(0);
  }

  window.AgiTour = { start, visible, agiSvg, place: () => place() };
})();
