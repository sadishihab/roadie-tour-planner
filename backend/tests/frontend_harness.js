// Test harness for frontend/index.html: runs the inline script against a tiny fake DOM and prints JSON.
// usage: node frontend_harness.js <index.html> <input.json>   (no network, no real DOM needed)
'use strict';
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];

function Text(data) { this.nodeType = 3; this.data = data; }
function El(tag) { this.nodeType = 1; this.tagName = tag; this.children = []; this.className = ''; this.listeners = {}; this.attrs = {}; this.open = false; }
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.removeChild = function (c) { this.children.splice(this.children.indexOf(c), 1); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.addEventListener = function (n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); };
El.prototype.click = function () { (this.listeners.click || []).forEach((f) => f()); };
El.prototype.scrollIntoView = function () { this.scrolled = true; };
El.prototype.focus = function () { this.focused = true; };
Object.defineProperty(El.prototype, 'firstChild', { get() { return this.children[0] || null; } });
Object.defineProperty(El.prototype, 'textContent', {
  get() { return this.children.map((c) => (c.nodeType === 3 ? c.data : c.textContent)).join(''); },
  set(v) { this.children = [new Text(String(v))]; },
});
const doc = {
  createElement: (t) => new El(t),
  createTextNode: (t) => new Text(String(t)),
  getElementById: () => new El('div'),
  activeElement: null,
};
const win = { __ROADIE_TEST__: {}, scrollTo() {} };
new Function('window', 'document', 'fetch', script)(win, doc, () => new Promise(() => {}));
const api = win.__ROADIE_TEST__.exports;

function tree(n) {
  if (n.nodeType === 3) return { x: n.data };
  return { t: n.tagName, c: n.className, k: n.children.map(tree), n: n.open ? 1 : 0 };
}
const out = {};
out.tierSummary = (input.tierLists || []).map((l) => api.tierSummary(l));
out.needsBadge = (input.badgeChecks || []).map(([l, t]) => api.needsTierBadge(api.tierSummary(l), t));
out.galleryText = (input.galleryItems || []).map((it) => api.galleryCard(it).textContent);
out.cleaned = (input.copy || []).map(api.cleanCopy);
out.plans = (input.entries || []).map((e) => {
  const root = new El('div');
  api.renderPlan(root, e, e.mode || 'gallery');
  return tree(root);
});
// clicking a route card opens and scrolls to its city section
out.clicks = (input.entries || []).map((e) => {
  const root = new El('div');
  api.renderPlan(root, e, 'gallery');
  const buttons = [];
  (function walk(n) { if (n.nodeType === 1) { if (n.tagName === 'button' && n.className === 'stop') buttons.push(n); n.children.forEach(walk); } })(root);
  const details = [];
  (function walk(n) { if (n.nodeType === 1) { if (n.tagName === 'details') details.push(n); n.children.forEach(walk); } })(root);
  const before = details.map((d) => d.open);
  if (buttons.length) buttons[buttons.length - 1].click();
  return { buttons: buttons.length, before, after: details.map((d) => d.open), scrolled: details.map((d) => !!d.scrolled) };
});
process.stdout.write(JSON.stringify(out));
