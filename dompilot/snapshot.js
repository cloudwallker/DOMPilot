// Shared semantic extraction for capture and stale-target checks.
// This source is evaluated in the page; it never reads body.innerText or innerHTML.
const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const clean = (value, limit) => normalize(value).slice(0, limit);

function safeHref(node) {
  if (!node.hasAttribute('href')) return true;
  try {
    const parsed = new URL(node.getAttribute('href'), node.baseURI);
    return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password;
  } catch (_) {
    return false;
  }
}

function visible(node) {
  if (!(node instanceof Element) || !node.isConnected) return false;
  for (let parent = node; parent instanceof Element; parent = parent.parentElement) {
    if (parent.hidden || parent.inert || parent.getAttribute('aria-hidden') === 'true') return false;
    const css = getComputedStyle(parent);
    if (css.display === 'none' || css.visibility === 'hidden' || css.visibility === 'collapse' ||
        Number(css.opacity) === 0) {
      return false;
    }
  }
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 &&
    rect.top < innerHeight && rect.left < innerWidth;
}

function available(node) {
  if (!visible(node) || node.matches(':disabled') || node.getAttribute('aria-disabled') === 'true' ||
      node.getAttribute('aria-readonly') === 'true') {
    return false;
  }
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    if (parent.getAttribute('aria-disabled') === 'true' ||
        parent.getAttribute('aria-readonly') === 'true') return false;
  }
  return true;
}

function kindAndActions(node) {
  const tag = node.tagName.toLowerCase();
  const role = (node.getAttribute('role') || '').toLowerCase();
  if (tag === 'input') {
    const type = (node.getAttribute('type') || 'text').toLowerCase();
    if (['checkbox', 'radio'].includes(type)) return [type, ['CLICK']];
    if (['text', 'search', 'email', 'url', 'tel'].includes(type) && !node.readOnly) {
      return [type, ['TYPE_TEXT']];
    }
    return null;
  }
  if (tag === 'textarea' && !node.readOnly) return ['textarea', ['TYPE_TEXT']];
  if (tag === 'select' && !node.multiple) return ['select', ['SELECT']];
  if (tag === 'button') return ['button', ['CLICK']];
  if (tag === 'a' && node.hasAttribute('href') && safeHref(node)) return ['link', ['CLICK']];
  if ((role === 'button' || role === 'link') && safeHref(node)) return [role, ['CLICK']];
  return null;
}

function nameOf(node) {
  const labelledby = node.getAttribute('aria-labelledby');
  if (labelledby) {
    const labels = labelledby.split(/\s+/).map(id => document.getElementById(id))
      .filter(Boolean).map(item => normalize(item.textContent)).filter(Boolean);
    if (labels.length) return normalize(labels.join(' '));
  }
  const aria = normalize(node.getAttribute('aria-label'));
  if (aria) return aria;
  if (node.labels && node.labels.length) {
    const labels = [...node.labels].map(item => normalize(item.textContent)).filter(Boolean);
    if (labels.length) return normalize(labels.join(' '));
  }
  const text = normalize(node.textContent);
  if (text) return text;
  return normalize(node.getAttribute('placeholder') || node.getAttribute('title'));
}

function signature(node) {
  const kind = kindAndActions(node);
  if (!kind || !available(node)) return null;
  return {
    tag: node.tagName.toLowerCase(), type: normalize(node.getAttribute('type')),
    role: normalize(node.getAttribute('role')), name: nameOf(node),
    href: normalize(node.getAttribute('href')), id: normalize(node.id),
    resolvedHref: node.tagName.toLowerCase() === 'a' ? node.href : '',
    fieldName: normalize(node.getAttribute('name')),
    placeholder: normalize(node.getAttribute('placeholder')),
    title: normalize(node.getAttribute('title')),
    kind: kind[0], multiple: Boolean(node.multiple), readOnly: Boolean(node.readOnly),
    value: String(node.value ?? ''), checked: Boolean(node.checked),
    options: node.tagName.toLowerCase() === 'select' ? [...node.options].map(option => ({
      value: option.value, label: normalize(option.label || option.textContent),
      disabled: option.disabled || Boolean(option.parentElement?.disabled)
    })) : []
  };
}

function challengeReason(feedback = null) {
  if ([...document.querySelectorAll('input[type="password"]')].some(visible)) {
    return 'visible_password_form';
  }
  if ([...document.querySelectorAll('iframe')].some(node =>
    visible(node) && /captcha|challenge|verification|verify|security check/i.test(
      [node.title, node.getAttribute('src'), node.getAttribute('aria-label')].join(' ')
    ))) return 'verification_challenge';
  if ([...document.querySelectorAll('input[autocomplete="one-time-code"]')].some(visible)) {
    return 'verification_challenge';
  }
  if (feedback === null) {
    const parts = [];
    for (const node of document.querySelectorAll('h1,h2,h3,[role="status"],[role="alert"],[aria-live]')) {
      if (visible(node)) parts.push(clean(node.textContent, 300));
      if (parts.join(' ').length >= 1000) break;
    }
    feedback = clean(parts.join(' '), 1000);
  }
  if (/captcha|verify you are human|security challenge|checking your browser|login verification|verification code|verify your account|two.factor|automated access is not allowed|人机验证|安全验证|验证码|自动化访问/i.test(feedback)) {
    return 'verification_challenge';
  }
  return null;
}

function capture() {
  const nodes = [], signatures = [], targets = [];
  let omitted = 0, omittedOptions = 0, totalOptions = 0;
  const candidates = document.querySelectorAll(
    'button,a[href],input,textarea,select,[role="button"],[role="link"]'
  );
  for (const node of candidates) {
    const sig = signature(node);
    if (!sig) continue;
    if (targets.length >= 50) { omitted++; continue; }
    const kind = kindAndActions(node);
    const options = [];
    if (kind[0] === 'select') {
      const seenValues = new Set();
      for (const option of node.options) {
        // Playwright selects the first matching value, including disabled options.
        if (seenValues.has(option.value)) { omittedOptions++; continue; }
        seenValues.add(option.value);
        if (option.disabled || option.parentElement?.disabled) { omittedOptions++; continue; }
        if (options.length >= 20 || totalOptions >= 100 || option.value.length > 200) {
          omittedOptions++; continue;
        }
        options.push({value: option.value, label: clean(option.label || option.textContent, 120)});
        totalOptions++;
      }
    }
    const near = node.closest('fieldset,form,section,article');
    const legend = near && near.querySelector('legend,h1,h2,h3');
    const context = legend ? clean(legend.textContent, 200) : '';
    targets.push({
      id: targets.length + 1, kind: kind[0], name: clean(sig.name, 120),
      placeholder: clean(node.getAttribute('placeholder'), 120),
      value: String(node.value ?? '').slice(0, 200), context, actions: kind[1], options,
      checked: ['checkbox', 'radio'].includes(kind[0]) ? Boolean(node.checked) : null,
      href: clean(sig.href, 200)
    });
    nodes.push(node);
    signatures.push(sig);
  }
  const contextParts = [];
  for (const node of document.querySelectorAll('h1,h2,h3,[role="status"],[role="alert"],[aria-live]')) {
    if (!visible(node)) continue;
    const text = clean(node.textContent, 300);
    if (text && !contextParts.includes(text)) contextParts.push(text);
    if (contextParts.join(' ').length >= 1000) break;
  }
  const title = clean(document.title, 200);
  const feedback = clean(contextParts.join(' | '), 1000 - title.length);
  const blockedReason = challengeReason(feedback);
  const scrollY = window.scrollY;
  const viewportHeight = window.innerHeight;
  const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
  return {
    data: {url: location.href, title, targets, feedback,
      scroll_y: scrollY, viewport_height: viewportHeight, can_scroll_up: scrollY > 1,
      can_scroll_down: scrollY + viewportHeight < scrollHeight - 1,
      omitted, omitted_options: omittedOptions, blocked_reason: blockedReason},
    nodes, signatures
  };
}
