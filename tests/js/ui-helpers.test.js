'use strict';

// Unit tests for small pure UI helpers, extracted from the admin assets.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const assets = path.join(__dirname, '..', '..', 'src', 'static', 'assets');
const read = name => fs.readFileSync(path.join(assets, name), 'utf8');

// Top-level functions in these assets end with a "}" at column 0.
function extract(source, name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `function ${name} not found`);
    const end = source.indexOf('\n}', start);
    assert.ok(end > start, `unterminated function ${name}`);
    return source.slice(start, end + 2);
}

test('avatar initials ignore bracketed notes and punctuation', () => {
    const initials = new Function(`${extract(read('common.js'), 'initials')}; return initials;`)();
    assert.equal(initials('林晓（测试）', ''), '林晓');
    assert.equal(initials('林晓', ''), '林晓');
    assert.equal(initials('欧阳娜娜', ''), '娜娜');
    assert.equal(initials('Sofia Silva', ''), 'SS');
    assert.equal(initials('Omar', ''), 'OM');
    assert.equal(initials('John (Acme)', ''), 'JO');
    assert.equal(initials('', '+8613800001234'), '34');
    assert.equal(initials('+86 138 0000 1234', '+8613800001234'), '34', 'a phone number used as name falls back to digits');
});

test('message source badges distinguish AI, phone, console and media', () => {
    const src = read('conversations.js');
    const badge = new Function(
        "const MEDIA_MESSAGE_TYPES = ['image','audio','video','document','sticker','location','contact'];" +
        `${extract(src, 'messageSourceBadge')}; return messageSourceBadge;`)();
    assert.match(badge({ direction: 'outbound', ai_generated: 1 }), />AI</);
    assert.match(badge({ direction: 'outbound', ai_generated: 0, send_kind: 'manual', message_type: 'phone' }), />手机</);
    assert.match(badge({ direction: 'outbound', ai_generated: 0, send_kind: 'manual', message_type: 'text' }), />人工</);
    assert.match(badge({ direction: 'outbound', ai_generated: 0, send_kind: 'proactive' }), />跟进</);
    assert.match(badge({ direction: 'inbound', message_type: 'image' }), />需人工查看</);
    assert.equal(badge({ direction: 'inbound', message_type: 'text' }), '');
});

test('activity log groups consecutive duplicates newest first', () => {
    const src = read('activity.js');
    const group = new Function(
        `${extract(src, 'parseLogLine')}; ${extract(src, 'groupActivityEntries')}; return groupActivityEntries;`)();
    const rows = group([
        '2026-11-01 10:00:00 [INFO] started',
        '2026-11-01 10:00:01 [WARNING] key missing',
        '2026-11-01 10:00:02 [WARNING] key missing',
        '2026-11-01 10:00:03 [ERROR] boom',
    ]);
    assert.deepEqual(rows.map(r => [r.level, r.msg, r.count]), [
        ['ERROR', 'boom', 1],
        ['WARNING', 'key missing', 2],
        ['INFO', 'started', 1],
    ]);
    assert.equal(rows[1].time, '2026-11-01 10:00:02');
    assert.equal(rows[1].firstTime, '2026-11-01 10:00:01');
});
