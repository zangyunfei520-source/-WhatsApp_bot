'use strict';

// Pure helpers for the inbound side of the bridge, kept free of Baileys so
// they can be unit-tested with `node --test`.

function unwrapMessage(rawMsg) {
    let m = rawMsg;
    for (let i = 0; i < 4 && m; i++) {
        if (m.ephemeralMessage?.message) { m = m.ephemeralMessage.message; continue; }
        if (m.viewOnceMessage?.message) { m = m.viewOnceMessage.message; continue; }
        if (m.viewOnceMessageV2?.message) { m = m.viewOnceMessageV2.message; continue; }
        if (m.documentWithCaptionMessage?.message) { m = m.documentWithCaptionMessage.message; continue; }
        break;
    }
    return m || null;
}

const MEDIA_LABELS = {
    image: '[图片]',
    audio: '[语音]',
    video: '[视频]',
    document: '[文件]',
    sticker: '[贴纸]',
    location: '[位置]',
    contact: '[联系人名片]',
};

/**
 * Classify a WhatsApp message.
 * Returns { type, text } where type is 'text' or one of the MEDIA_LABELS keys.
 * Media with a caption keeps the caption as text but is still reported as media
 * so the backend knows the customer sent something the AI cannot see.
 * Returns null for protocol/system messages that should be ignored.
 */
function classifyMessage(rawMsg) {
    const m = unwrapMessage(rawMsg);
    if (!m) return null;

    const plain = m.conversation || m.extendedTextMessage?.text ||
        m.buttonsResponseMessage?.selectedDisplayText || m.listResponseMessage?.title || '';
    if (plain && plain.trim()) return { type: 'text', text: plain };

    let type = null;
    let caption = '';
    let detail = '';
    if (m.imageMessage) { type = 'image'; caption = m.imageMessage.caption || ''; }
    else if (m.videoMessage) { type = 'video'; caption = m.videoMessage.caption || ''; }
    else if (m.audioMessage) { type = 'audio'; }
    else if (m.documentMessage) {
        type = 'document';
        caption = m.documentMessage.caption || '';
        detail = m.documentMessage.fileName || '';
    }
    else if (m.stickerMessage) { type = 'sticker'; }
    else if (m.locationMessage || m.liveLocationMessage) {
        type = 'location';
        const loc = m.locationMessage || m.liveLocationMessage;
        if (loc && loc.degreesLatitude != null && loc.degreesLongitude != null) {
            detail = `${loc.degreesLatitude},${loc.degreesLongitude}`;
        }
    }
    else if (m.contactMessage || m.contactsArrayMessage) {
        type = 'contact';
        detail = m.contactMessage?.displayName || '';
    }
    if (!type) return null;

    const label = MEDIA_LABELS[type] + (detail ? ` ${detail}` : '');
    return { type, text: caption.trim() ? `${label} ${caption.trim()}` : label };
}

/** Remembers ids of messages this bridge sent, so their echoes are not mistaken for human replies. */
function createSentIdTracker(ttlMs = 10 * 60 * 1000, maxSize = 5000) {
    const ids = new Map();
    function prune(now) {
        for (const [id, ts] of ids) {
            if (now - ts <= ttlMs && ids.size <= maxSize) break;
            ids.delete(id);
        }
    }
    return {
        add(id, now = Date.now()) { if (id) { ids.set(id, now); prune(now); } },
        has(id, now = Date.now()) {
            if (!id || !ids.has(id)) return false;
            if (now - ids.get(id) > ttlMs) { ids.delete(id); return false; }
            return true;
        },
        get size() { return ids.size; },
    };
}

/**
 * Per-chat debounce: collects quick consecutive messages and flushes them
 * together once the customer stops typing for `quietMs` (but never waits longer
 * than `maxWaitMs` after the first message).
 */
function createBatcher(onFlush, { quietMs = 6000, maxWaitMs = 20000, timers = { setTimeout, clearTimeout }, now = Date.now } = {}) {
    const pending = new Map();
    function flush(key) {
        const entry = pending.get(key);
        if (!entry) return;
        pending.delete(key);
        timers.clearTimeout(entry.timer);
        onFlush(key, entry.items);
    }
    return {
        push(key, item) {
            if (quietMs <= 0) { onFlush(key, [item]); return; }
            let entry = pending.get(key);
            if (!entry) { entry = { items: [], first: now(), timer: null }; pending.set(key, entry); }
            entry.items.push(item);
            if (entry.timer) timers.clearTimeout(entry.timer);
            const remaining = Math.max(0, entry.first + maxWaitMs - now());
            entry.timer = timers.setTimeout(() => flush(key), Math.min(quietMs, remaining));
        },
        flushAll() { for (const key of Array.from(pending.keys())) flush(key); },
        get size() { return pending.size; },
    };
}

/** Build the sequence of webhook payloads for one flushed batch. */
function batchPayloads(phone, items) {
    const textParts = items.filter(i => i.type === 'text').map(i => i.text.trim()).filter(Boolean);
    return items.map((item, idx) => {
        const last = idx === items.length - 1;
        const payload = {
            from: phone,
            body: item.text,
            msg_id: item.msgId || null,
            timestamp: item.timestamp != null ? String(item.timestamp) : null,
            message_type: item.type,
            defer_reply: !last,
        };
        if (last && items.length > 1 && textParts.length) payload.batch_text = textParts.join('\n');
        return payload;
    });
}

function generateMessageId(randomBytes) {
    return '3EB0' + randomBytes(9).toString('hex').toUpperCase();
}

module.exports = {
    MEDIA_LABELS,
    unwrapMessage,
    classifyMessage,
    createSentIdTracker,
    createBatcher,
    batchPayloads,
    generateMessageId,
};
