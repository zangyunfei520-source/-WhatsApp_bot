'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('crypto');
const {
    classifyMessage,
    createSentIdTracker,
    createBatcher,
    batchPayloads,
    generateMessageId,
} = require('../lib/inbound');

test('classifyMessage returns plain text', () => {
    assert.deepEqual(classifyMessage({ conversation: 'hello' }), { type: 'text', text: 'hello' });
    assert.deepEqual(classifyMessage({ extendedTextMessage: { text: 'link' } }), { type: 'text', text: 'link' });
    assert.deepEqual(
        classifyMessage({ ephemeralMessage: { message: { conversation: 'wrapped' } } }),
        { type: 'text', text: 'wrapped' });
});

test('classifyMessage turns media into a placeholder instead of dropping it', () => {
    assert.deepEqual(classifyMessage({ imageMessage: {} }), { type: 'image', text: '[图片]' });
    assert.deepEqual(classifyMessage({ audioMessage: { ptt: true } }), { type: 'audio', text: '[语音]' });
    assert.deepEqual(
        classifyMessage({ imageMessage: { caption: 'nameplate' } }),
        { type: 'image', text: '[图片] nameplate' });
    assert.deepEqual(
        classifyMessage({ documentMessage: { fileName: 'spec.pdf' } }),
        { type: 'document', text: '[文件] spec.pdf' });
    assert.deepEqual(
        classifyMessage({ locationMessage: { degreesLatitude: 1.5, degreesLongitude: 2.5 } }),
        { type: 'location', text: '[位置] 1.5,2.5' });
});

test('classifyMessage ignores protocol messages', () => {
    assert.equal(classifyMessage({ protocolMessage: { type: 0 } }), null);
    assert.equal(classifyMessage(null), null);
    assert.equal(classifyMessage({ conversation: '   ' }), null);
});

test('sent id tracker remembers ids within the ttl', () => {
    const t = createSentIdTracker(1000, 3);
    t.add('a', 0);
    assert.equal(t.has('a', 500), true);
    assert.equal(t.has('a', 1500), false);
    assert.equal(t.has('missing', 0), false);
    t.add('b', 0); t.add('c', 0); t.add('d', 0); t.add('e', 0);
    assert.ok(t.size <= 3);
    assert.equal(t.has('e', 1), true);
});

test('generateMessageId has the Baileys shape and is unique', () => {
    const a = generateMessageId(crypto.randomBytes);
    const b = generateMessageId(crypto.randomBytes);
    assert.match(a, /^3EB0[0-9A-F]{18}$/);
    assert.notEqual(a, b);
});

function fakeClock() {
    let now = 0; let seq = 0; const timers = new Map();
    return {
        now: () => now,
        timers: {
            setTimeout(fn, ms) { const id = ++seq; timers.set(id, { fn, at: now + ms }); return id; },
            clearTimeout(id) { timers.delete(id); },
        },
        advance(ms) {
            now += ms;
            for (const [id, t] of Array.from(timers)) {
                if (t.at <= now) { timers.delete(id); t.fn(); }
            }
        },
    };
}

test('batcher merges quick consecutive messages and respects max wait', () => {
    const clock = fakeClock();
    const flushed = [];
    const b = createBatcher((key, items) => flushed.push([key, items.map(i => i.text)]),
        { quietMs: 5000, maxWaitMs: 12000, timers: clock.timers, now: clock.now });
    b.push('j1', { text: 'hi' });
    clock.advance(3000);
    b.push('j1', { text: 'need 1MW genset' });
    clock.advance(4000);
    assert.equal(flushed.length, 0, 'still typing within quiet window');
    clock.advance(1000);
    assert.deepEqual(flushed, [['j1', ['hi', 'need 1MW genset']]]);

    // A customer who keeps typing is flushed after maxWaitMs anyway.
    for (let i = 0; i < 5; i++) { b.push('j2', { text: `m${i}` }); clock.advance(3000); }
    assert.equal(flushed.length, 2);
    assert.deepEqual(flushed[1][1].slice(0, 4), ['m0', 'm1', 'm2', 'm3']);
});

test('batcher with quietMs=0 forwards immediately', () => {
    const flushed = [];
    const b = createBatcher((key, items) => flushed.push(items.length), { quietMs: 0 });
    b.push('j', { text: 'a' });
    assert.deepEqual(flushed, [1]);
});

test('batchPayloads defers all but the last message and joins the text', () => {
    const payloads = batchPayloads('8613800000000', [
        { text: 'hi', type: 'text', msgId: 'A', timestamp: 1 },
        { text: '[图片]', type: 'image', msgId: 'B', timestamp: 2 },
        { text: 'price?', type: 'text', msgId: 'C', timestamp: 3 },
    ]);
    assert.deepEqual(payloads.map(p => p.defer_reply), [true, true, false]);
    assert.equal(payloads[2].batch_text, 'hi\nprice?');
    assert.equal(payloads[1].message_type, 'image');
    assert.equal(payloads[0].batch_text, undefined);

    const single = batchPayloads('1', [{ text: 'solo', type: 'text', msgId: 'X' }]);
    assert.equal(single[0].defer_reply, false);
    assert.equal(single[0].batch_text, undefined);
});
