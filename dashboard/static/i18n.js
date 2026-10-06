/* Source-keyed EN/RU catalogs. Interpolation happens after translating trusted
   markup, so player names, field values and server output are never translated. */
"use strict";
window.I18n = (() => {
  const storageKey = "pz-language";
  const normalize = value => String(value || "").toLowerCase().split(/[-_]/)[0];
  const supported = value => ["en", "ru"].includes(normalize(value));
  let stored;
  try { stored = localStorage.getItem(storageKey); } catch { /* Private browsing. */ }
  const cookie = document.cookie.match(/(?:^|;\s*)pz_language=(en|ru)(?:;|$)/)?.[1];
  const detected = (navigator.languages || [navigator.language]).find(supported);
  let language, locale, catalog, plurals;
  function activate(value) {
    language = value;
    locale = language === "ru" ? "ru-RU" : "en-US";
    catalog = window.PZ_TRANSLATIONS?.[language] || {};
    plurals = new Intl.PluralRules(locale);
    document.documentElement.lang = language;
    const manifest = document.querySelector('link[rel="manifest"]');
    if (manifest) manifest.href = `/manifest.webmanifest?lang=${language}`;
  }
  activate(normalize(supported(stored) ? stored : cookie || detected || "en"));
  const sourceTexts = new WeakMap();
  const sourceAttributes = new WeakMap();
  const boundSwitches = new WeakSet();
  const pattern = /{{(\d+)}}/g;
  const canonical = text => String(text).replace(/\s+/g, " ").trim();

  function t(source, values = [], interpolate = true) {
    source = String(source ?? "");
    const key = canonical(source);
    let result = catalog[key] ?? key;
    if (typeof result === "object") result = result[plurals.select(Number(values[0]))] || result.other;
    // Preserve source whitespace and line breaks in the original language.
    if (language === "ru" && !Object.hasOwn(catalog, key)) result = source;
    else result = (source.match(/^\s*/)?.[0] || "") + result + (source.match(/\s*$/)?.[0] || "");
    return interpolate ? result.replace(pattern, (match, index) => index in values ? String(values[index] ?? "") : match) : result;
  }

  function html(source, values = [], interpolate = true) {
    if (Object.hasOwn(catalog, canonical(source))) return t(source, values, interpolate);
    // Only catalogued text and human-facing attributes in authored markup.
    return String(source).split(/(<[^>]*>)/g).map(part => {
      if (!part.startsWith("<")) return t(part, values, interpolate);
      return part.replace(/\b(title|aria-label|aria-description|placeholder|data-help-text|data-help-label)=(['"])(.*?)\2/gs,
        (_, name, quote, text) => `${name}=${quote}${t(text, values, interpolate)}${quote}`);
    }).join("");
  }

  function msg(strings, ...values) {
    const source = strings.reduce((text, part, index) => text + part + (index < values.length ? `{{${index}}}` : ""), "");
    // Translate before substituting values, including strings that look like HTML.
    // Substitute once: user values containing {{0}} must remain literal.
    const translated = html(source, values, false);
    return translated.replace(pattern, (match, index) => index in values ? String(values[index] ?? "") : match);
  }

  function translateDocument() {
    const walker = document.createTreeWalker(document, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      // Language names stay recognizable in their own language in every locale.
      if (node.parentElement?.closest("script,style,textarea,pre,code,option[lang]")) continue;
      if (!sourceTexts.has(node) && Object.hasOwn(window.PZ_TRANSLATIONS?.en || {}, canonical(node.nodeValue))) sourceTexts.set(node, node.nodeValue);
      if (sourceTexts.has(node)) node.nodeValue = t(sourceTexts.get(node));
    }
    for (const node of document.querySelectorAll("*")) {
      if (!sourceAttributes.has(node)) sourceAttributes.set(node, {});
      const originals = sourceAttributes.get(node);
      for (const name of ["title", "aria-label", "placeholder", "aria-description", "data-help-text", "data-help-label"]) {
        if (node.hasAttribute(name)) {
          originals[name] ??= node.getAttribute(name);
          node.setAttribute(name, t(originals[name]));
        }
      }
    }
    for (const node of document.querySelectorAll('meta[name="apple-mobile-web-app-title"]')) {
      const originals = sourceAttributes.get(node);
      originals.content ??= node.content;
      node.content = t(originals.content);
    }
    for (const node of document.querySelectorAll('[data-language-switch]')) {
      node.value = language;
      node.setAttribute("aria-label", t("Язык интерфейса"));
      if (boundSwitches.has(node)) continue;
      boundSwitches.add(node);
      node.addEventListener("change", async () => {
        const next = node.value;
        node.disabled = true;
        try {
          // Standalone pages can change language in place. Credentials and
          // offline retry state remain in memory, without storage or reload.
          if (!document.getElementById("view-overview")) {
            if (document.getElementById("loginSubmit")?.disabled) throw new Error(t("Сохраните ввод и завершите действие перед сменой языка."));
            persist(next);
            activate(next);
            translateDocument();
            const error = document.getElementById("loginError");
            if (error) error.hidden = true;
            const status = document.getElementById("languageStatus");
            if (status) status.hidden = true;
            document.dispatchEvent(new CustomEvent("pz:language-changed"));
            return;
          }
          await window.ConfigEditor?.prepareLanguageChange();
          const event = new CustomEvent("pz:before-update", { cancelable: true });
          document.dispatchEvent(event);
          if (event.defaultPrevented || document.querySelector("dialog[open]") ||
              document.getElementById("loginSubmit")?.disabled ||
              [...document.querySelectorAll('[data-language-dirty]')].some(el => el.value)) {
            throw new Error(t("Сохраните ввод и завершите действие перед сменой языка."));
          }
          persist(next);
          location.reload();
        } catch (error) {
          node.value = language;
          const notice = document.getElementById("languageStatus");
          if (notice) { notice.textContent = error.message; notice.hidden = false; }
        } finally { node.disabled = false; }
      });
    }
  }

  document.addEventListener("input", event => {
    const el = event.target;
    if (!el.matches('input:not([type="checkbox"]):not([type="radio"]),textarea') ||
        el.closest("#configFields") || /^(ini|sandbox)Source$/.test(el.id) ||
        el.type === "search" || el.readOnly ||
        el.closest("#sec-updates,#sec-watchdog,#sec-telegram,#sec-bkauto,#sec-modscheck")) return;
    el.setAttribute("data-language-dirty", "");
  });

  function saveCookie(value) {
    document.cookie = `pz_language=${value}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
  }
  function persist(value) {
    try { localStorage.setItem(storageKey, value); } catch { /* Cookie fallback. */ }
    saveCookie(value);
  }
  saveCookie(language);
  return Object.freeze({
    get language() { return language; },
    get locale() { return locale; },
    t, html, msg, translateDocument,
    number: (value, options) => new Intl.NumberFormat(locale, options).format(Number(value)),
    count: (key, count) => t(key, [count], false).replace(/\{\{0\}\}/g, new Intl.NumberFormat(locale).format(count)),
  });
})();
