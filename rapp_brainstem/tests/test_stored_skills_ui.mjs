import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { File } from 'node:buffer';

const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(match => match[1]);
for (const script of scripts) new vm.Script(script);
const source = scripts.join('\n');
assert(!source.includes('sessionSkills'));
assert(!source.includes('session_skills'));
const state = source.match(/  let sessionId = null;[\s\S]*?  let conversationEpoch = 0;/)[0];
const functions = ['loadSkillsList', 'convertSkill', 'clearChat', 'importChat', 'installRarAgent']
  .map(name => source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}`))[0])
  .join('\n');
const drop = source.match(/window\.addEventListener\('drop', async \(e\) => \{[\s\S]*?\n  \}\);/)[0];

class Element {
  constructor() {
    this.children = [];
    this.style = {};
    this.classList = { add() {}, remove() {} };
    this.disabled = false;
    this.value = '';
    this._text = '';
  }
  set innerHTML(value) { this.children = []; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  querySelector() { return new Element(); }
  remove() {}
  focus() {}
}

const skill = { name: 'workshop-helper', filename: 'workshop-helper.md', description: 'Stored Markdown' };
const markdown = '---\nname: workshop-helper\n---\nDo the requested task.';
let files = [];
const requests = [];
let conversionFails = false;

async function server(url, options) {
  requests.push({ url, options });
  if (url === 'http://test/skills') return { ok: true, json: async () => ({ files: files.map(file => ({ ...file })) }) };
  if (url === 'http://test/skills/export/workshop-helper.md') return {
    ok: files.length > 0, blob: async () => new Blob([markdown], { type: 'text/markdown' }),
  };
  if (url === 'http://test/skills/workshop-helper.md' && options.method === 'DELETE') {
    files = [];
    return { ok: true, json: async () => ({ status: 'ok' }) };
  }
  if (url === 'http://test/skills/import') {
    const mode = options.body.get('mode');
    if (mode === 'agent') {
      assert.equal(await options.body.get('file').text(), markdown);
      return { ok: !conversionFails, json: async () => conversionFails
        ? { error: 'Generation failed' }
        : { status: 'ok', scope: 'persistent', filename: 'workshop_helper_agent.py' } };
    }
    assert.equal(mode, null);
    files = [{ ...skill }];
    return { ok: true, json: async () => ({ status: 'ok', scope: 'skill', filename: skill.filename, skill }) };
  }
  if (url === 'http://test/chat') {
    const body = JSON.parse(options.body);
    assert(!('session_skills' in body));
    return { json: async () => ({ response: 'Ready.', session_id: 'chat' }) };
  }
  if (url.startsWith('https://registry.invalid/')) return { ok: true, text: async () => markdown };
  throw new Error(`Unexpected request ${url}`);
}

function page() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    API: 'http://test', RAR_BASE: 'https://registry.invalid', RAR_REVISION: 'a'.repeat(40),
    Blob, FormData, fetch: server,
    rarFetch: async () => ({ ok: true, arrayBuffer: async () => new TextEncoder().encode(markdown).buffer }),
    sha256Hex: async () => 'b'.repeat(64),
    document: { getElementById: element, createElement: () => new Element() },
    input: element('input'), dropOverlay: element('drop-overlay'),
    window: {
      addEventListener: (event, handler) => { context.drop = handler; },
      open: url => context.opened.push(url),
    },
    alert: message => context.alerts.push(message), alerts: [], opened: [], confirm: () => true,
    loadAgentsList() {}, appendMsg: () => new Element(), appendTyping() {}, removeTyping() {},
    clog() {}, console: { log() {} }, voiceMode: false, speakText() {}, cancelActiveRequests() {},
    starterPayloadOverride: null,
    localStorage: { setItem() { assert.fail('Skills belong on disk, not in browser storage'); } },
  });
  vm.runInContext(state + '\n' + functions + '\n' + drop, context);
  context.element = element;
  return context;
}

const first = page();
await first.drop({ preventDefault() {}, dataTransfer: { files: [new File([markdown], 'SKILL (17).md')] } });
assert(first.alerts[0].includes('Saved skill:'));
assert.equal(files.length, 1);
await first.loadSkillsList();
assert.equal(first.element('skill-list-ul').children[0].children[0].textContent, skill.name);
first.clearChat();
assert.equal(files.length, 1, 'Clear must not delete Markdown');

const refreshed = page();
await refreshed.loadSkillsList();
const row = refreshed.element('skill-list-ul').children[0];
assert.equal(row.children[0].textContent, skill.name);
assert.equal(files.length, 1);

const [convert, download, remove] = row.children[1].children;
assert.equal(convert.textContent, 'Convert to agent');
download.onclick();
assert.equal(refreshed.opened[0], 'http://test/skills/export/workshop-helper.md');
await refreshed.convertSkill(skill, convert);
assert.equal(files.length, 1, 'Conversion keeps the Markdown source');
assert(requests.some(request => request.options?.body?.get?.('mode') === 'agent'));
conversionFails = true;
await refreshed.convertSkill(skill, convert);
assert.equal(files.length, 1);
assert.equal(convert.disabled, false);
assert(refreshed.alerts.some(message => message.includes('Generation failed')));

await remove.onclick();
assert.equal(files.length, 0);
assert.equal(refreshed.element('skill-list-ul').textContent, 'No Markdown skills installed.');
const third = page();
await third.loadSkillsList();
assert.equal(third.element('skill-list-ul').textContent, 'No Markdown skills installed.');

const button = new Element();
button.textContent = 'Add';
await third.installRarAgent({ _file: 'skills/workshop/SKILL.md', _sha256: 'b'.repeat(64) }, button);
assert.equal(button.textContent, 'Skill saved');
assert.equal(files.length, 1);
third.FileReader = class {
  readAsText(file) { this.onload({ target: { result: file.content } }); }
};
third.importChat({ target: { value: 'chat.json', files: [{
  content: JSON.stringify({ turns: [{ role: 'user', content: 'Another conversation' }] }),
}] } });
assert.equal(files.length, 1, 'Importing a conversation must not delete skills');
console.log('PASS persistent drops, refresh, clear/import, export/delete, and explicit conversion');
