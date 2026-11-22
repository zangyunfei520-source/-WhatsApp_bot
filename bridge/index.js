const express = require('express');
const qrcode = require('qrcode-terminal');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const sendResult = require('./lib/send-result');
const inbound = require('./lib/inbound');

let makeWASocket, useMultiFileAuthState, DisconnectReason;

const BRIDGE_PORT = parseInt(process.env.BRIDGE_PORT || '3001');
const BRIDGE_HOST = process.env.BRIDGE_HOST || '127.0.0.1';
const PYTHON_API = process.env.PYTHON_API || 'http://127.0.0.1:8000';
const DATA_DIR = process.env.WHATSAPP_BOT_DATA_DIR || path.join(__dirname, '..');
const AUTH_DIR = path.join(DATA_DIR, 'data', '.baileys-auth');
const TENANT_ID = process.env.WA_TENANT_ID || '';
const BRIDGE_TOKEN = process.env.WA_BRIDGE_TOKEN || '';
// Quick consecutive messages from one customer are merged into one AI reply.
const MESSAGE_BATCH_MS = Math.max(0, parseInt(process.env.MESSAGE_BATCH_MS || '6000', 10) || 0);
const MESSAGE_BATCH_MAX_MS = Math.max(MESSAGE_BATCH_MS, parseInt(process.env.MESSAGE_BATCH_MAX_MS || '20000', 10) || 0);
// Messages you send from the phone older than this (e.g. history sync after reconnect) are ignored.
const OWN_MESSAGE_MAX_AGE_S = 10 * 60;
const DEBUG = process.env.BRIDGE_DEBUG === '1';
const TENANT_HEADERS = {
    ...(TENANT_ID ? { 'X-Tenant-Id': TENANT_ID } : {}),
    ...(BRIDGE_TOKEN ? { 'X-Bridge-Token': BRIDGE_TOKEN } : {}),
};

const app = express();
// /send-document accepts up to 50MB raw files (~67MB base64 JSON); keep other
// JSON endpoints tightly capped while still above Express's 100KB default.
app.use('/send-document', express.json({ limit: '75mb' }));
app.use(express.json({ limit: '1mb' }));
app.use((err, _req, res, next) => {
    if (err && err.type === 'entity.too.large') {
        return res.status(413).json({ status: 'failed', error: '请求体过大', error_code: 'payload_too_large' });
    }
    return next(err);
});

function hasValidBridgeToken(req) {
    if (!BRIDGE_TOKEN) return true;
    const provided = req.get('X-Bridge-Token') || '';
    const a = Buffer.from(provided, 'utf8');
    const b = Buffer.from(BRIDGE_TOKEN, 'utf8');
    if (a.length !== b.length) return false;
    return crypto.timingSafeEqual(a, b);
}

app.use((req, res, next) => {
    if (req.path === '/health') return next();
    if (!hasValidBridgeToken(req)) {
        return res.status(401).json({ status: 'failed', error: 'unauthorized bridge request', error_code: 'unauthorized' });
    }
    return next();
});

let sock = null, clientReady = false, isShutdown = false;
const ownSentIds = inbound.createSentIdTracker();
const contacts = new Map();
const MAX_CONTACTS = 10000;

function setContact(phone, data) {
    contacts.set(phone, data);
    // Evict oldest entries if over limit (Map maintains insertion order)
    while (contacts.size > MAX_CONTACTS) {
        const oldest = contacts.keys().next().value;
        contacts.delete(oldest);
    }
}
let reconnectAttempt = 0;
const BACKOFF_BASE = 2000, BACKOFF_MAX = 300_000;
let backoffTimer = null;

function resetBackoff() { reconnectAttempt = 0; if (backoffTimer) { clearTimeout(backoffTimer); backoffTimer = null; } }
function getBackoffDelay() {
    reconnectAttempt++;
    const d = Math.min(BACKOFF_BASE * Math.pow(2, reconnectAttempt - 1), BACKOFF_MAX);
    return Math.round(d * (1 + 0.25 * (Math.random() * 2 - 1)));
}
function getProxyAgent() {
    const u = process.env.HTTPS_PROXY || process.env.https_proxy;
    if (!u) return undefined;
    try { const { HttpsProxyAgent } = require('https-proxy-agent'); console.log(`Using proxy: ${u}`); return new HttpsProxyAgent(u); }
    catch (e) { console.error('Proxy agent failed:', e.message); return undefined; }
}

async function notifyPython(path, body, options = {}) {
    const retries = Math.max(0, options.retries || 0);
    for (let attempt = 0; attempt <= retries; attempt++) {
        try {
            const r = await fetch(`${PYTHON_API}${path}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...TENANT_HEADERS },
                body: JSON.stringify(body)
            });
            if (r.ok) return true;
        } catch (e) { /* ignore - Python may not be up yet */ }
        if (attempt < retries) {
            await new Promise(resolve => setTimeout(resolve, 500 * Math.pow(2, attempt)));
        }
    }
    return false;
}

async function sendText(phone, message, timeoutMs = 15000) {
    if (!clientReady || !sock) {
        return sendResult.failedResult('桥接未认证，请先扫码登录 WhatsApp', 'bridge_unauthenticated');
    }
    const cleaned = sendResult.cleanPhone(phone);
    if (!cleaned) {
        return sendResult.failedResult('清理后电话号码为空', 'invalid_phone');
    }
    const jid = cleaned.includes('@') ? cleaned : `${cleaned}@s.whatsapp.net`;
    // Pre-generate the id and remember it *before* sending: Baileys echoes our
    // own sends through messages.upsert, and those must not count as a human reply.
    const messageId = inbound.generateMessageId(crypto.randomBytes);
    ownSentIds.add(messageId);
    try {
        const sent = await Promise.race([
            sock.sendMessage(jid, { text: message }, { messageId }),
            sendResult.timeoutPromise(timeoutMs),
        ]);
        return sendResult.sentResult(sent, jid);
    } catch (e) {
        const result = sendResult.fromSendError(e);
        if (result.error_code === 'timeout') {
            const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
            result.error = proxy
                ? `消息发送超时（结果待确认）。已配置代理 ${proxy}，请检查代理与 WhatsApp 连接后人工核对。`
                : '消息发送超时（结果待确认）。未配置代理，请检查网络后人工核对。';
        }
        return result;
    }
}

async function showTyping(jid, seconds) {
    if (!sock || !clientReady || !(seconds > 0)) return;
    try {
        await sock.presenceSubscribe(jid);
        await sock.sendPresenceUpdate('composing', jid);
        await new Promise(resolve => setTimeout(resolve, Math.min(seconds, 8) * 1000));
        await sock.sendPresenceUpdate('paused', jid);
    } catch (_) { /* typing indicator is cosmetic */ }
}

async function forwardToPython(jid, payload) {
    const phone = payload.from;
    try {
        const r = await fetch(`${PYTHON_API}/webhook/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...TENANT_HEADERS },
            body: JSON.stringify(payload)
        });
        const result = await r.json();
        console.log(`[webhook] action=${result.action} reason=${result.reason || ''} conversation=${result.conversation_id || ''}`);
        if (result.action === 'reply' && result.message) {
            await showTyping(jid, Number(result.delay_seconds) || 0);
            const delivery = await sendText(phone, result.message, 15000);
            const receipt = {
                conversation_id: result.conversation_id || null,
                whatsapp_msg_id: delivery.message_id || null,
                status: delivery.status,
                error: delivery.error || null,
                error_code: delivery.error_code || null,
            };
            if (receipt.conversation_id) {
                const delivered = await notifyPython('/webhook/delivery', receipt, { retries: 3 });
                if (!delivered) console.error('[webhook] delivery receipt not acknowledged:', JSON.stringify(receipt));
            }
            console.log("[send] reply delivery status:", delivery.status);
            return { ...result, delivery_status: delivery.status };
        }
        return result;
    } catch (e) { console.error('[webhook] forward failed:', e.message, e.stack); return { action: 'ignore', reason: 'fetch failed' }; }
}

const jidQueues = new Map();

const batcher = inbound.createBatcher((jid, items) => {
    const phone = jid.replace(/@s\.whatsapp\.net$/, '').replace(/@c\.us$/, '');
    for (const payload of inbound.batchPayloads(phone, items)) {
        enqueueForJid(jid, () => forwardToPython(jid, payload));
    }
}, { quietMs: MESSAGE_BATCH_MS, maxWaitMs: MESSAGE_BATCH_MAX_MS });

function enqueueForJid(jid, task) {
    const prev = jidQueues.get(jid) || Promise.resolve();
    const next = prev
        .catch(() => {})
        .then(task)
        .finally(() => {
            if (jidQueues.get(jid) === next) {
                jidQueues.delete(jid);
            }
        });
    jidQueues.set(jid, next);
    return next;
}

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const opts = { auth: state, printQRInTerminal: false, connectTimeoutMs: 60_000, browser: ['WhatsApp Bot', 'Chrome', '2.0.0'] };
    const agent = getProxyAgent(); if (agent) opts.agent = agent;
    sock = makeWASocket(opts);

    sock.ev.on('creds.update', saveCreds);
    sock.ev.on('contacts.upsert', (items) => {
        for (const c of items) {
            const jid = c.id || c.jid || '';
            if (jid && !jid.endsWith('@g.us') && !jid.endsWith('@newsletter') && !jid.endsWith('@broadcast')) {
                const phone = jid.replace(/@s\.whatsapp\.net$/, '').replace(/@c\.us$/, '');
                const name = c.name || c.notify || c.verifiedName || '';
                setContact(phone, { phone, name });
            }
        }
    });
    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (qr) {
            console.log('>>> QR_RECEIVED <<<'); console.log('RAW_QR:' + qr); qrcode.generate(qr, { small: true }); console.log('>>> QR_END <<<');
            notifyPython('/webhook/qr', { qr_text: qr });
        }
        if (connection === 'open') {
            resetBackoff(); clientReady = true; console.log(`WHATSAPP_READY:${sock.user?.id?.split(':')[0] || 'unknown'}`);
            notifyPython('/webhook/qr', { qr_text: null });
        }
        if (connection === 'close') {
            clientReady = false;
            const code = lastDisconnect?.error?.output?.statusCode;
            console.log(`WHATSAPP_DISCONNECTED:${code || lastDisconnect?.error?.message || 'unknown'}`);
            if (isShutdown) return;
            if (code === DisconnectReason.loggedOut) {
                try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
                notifyPython('/webhook/qr', { qr_text: null });
                backoffTimer = setTimeout(start, getBackoffDelay());
                return;
            }
            backoffTimer = setTimeout(start, getBackoffDelay());
        }
    });

    sock.ev.on('messages.upsert', (msg) => {
        if (DEBUG) console.log('[debug] upsert', msg.type, msg.messages.length);
        if (isShutdown || !clientReady) return;
        for (const m of msg.messages) {
            if (!m.key) continue;
            const jid = m.key.remoteJid;
            if (!jid || jid === 'status@broadcast' || jid.endsWith('@g.us') || jid.endsWith('@newsletter')) continue;
            const p = jid.replace(/@s\.whatsapp\.net$/, '').replace(/@c\.us$/, '');
            const info = inbound.classifyMessage(m.message);
            if (!info) continue;

            if (m.key.fromMe) {
                // Our own sends echo back here; only messages typed by a human on
                // the phone / another linked device are reported.
                if (ownSentIds.has(m.key.id)) continue;
                const rawTs = m.messageTimestamp;
                const ts = (rawTs && typeof rawTs.toNumber === 'function' ? rawTs.toNumber() : Number(rawTs)) || 0;
                if (ts && Date.now() / 1000 - ts > OWN_MESSAGE_MAX_AGE_S) continue;
                if (msg.type && msg.type !== 'notify' && msg.type !== 'append') continue;
                enqueueForJid(jid, () => notifyPython('/webhook/outgoing', {
                    to: p, body: info.text, msg_id: m.key.id || null,
                    timestamp: ts ? String(ts) : null, message_type: info.type,
                }, { retries: 2 }));
                continue;
            }

            if (msg.type && msg.type !== 'notify') continue;
            if (!contacts.has(p)) {
                setContact(p, { phone: p, name: m.pushName || '' });
            }
            batcher.push(jid, { text: info.text, type: info.type, msgId: m.key.id, timestamp: m.messageTimestamp });
        }
    });
}

app.get('/health', (_, r) => r.json({ status: 'ok' }));
app.get('/chats', (_, r) => {
    const chatList = Array.from(contacts.values());
    r.json(chatList);
});
app.get('/status', (_, r) => r.json({ authenticated: clientReady, phone: sock?.user?.id?.split(':')[0] || null }));
app.post('/send', async (req, res) => {
    if (!clientReady || !sock) return res.status(503).json(sendResult.failedResult('桥接未认证，请先扫码登录 WhatsApp', 'bridge_unauthenticated'));
    let { phone, message } = req.body;
    if (!phone || !message) return res.status(400).json(sendResult.failedResult('电话号码和消息内容不能为空', 'invalid_request'));
    // ── Robust phone cleaning ──────────────────────────────
    const cleaned = sendResult.cleanPhone(phone);
    if (!cleaned) return res.status(400).json(sendResult.failedResult('清理后电话号码为空', 'invalid_phone'));
    phone = cleaned;
    const jid = phone.includes('@') ? phone : `${phone}@s.whatsapp.net`;
    if (DEBUG) console.log('[send] cleaned phone:', phone, '-> jid:', jid);
    const result = await sendText(phone, message, 15000);
    // Always return 200 with a structured status so the Python backend can
    // distinguish sent / failed / unknown. Status codes are reserved for
    // definite request-level failures.
    res.json(result);
});

// ── Document sending ──────────────────────────────────────
app.post('/send-document', async (req, res) => {
    if (!clientReady || !sock) return res.status(503).json({ status: 'failed', error: '桥接未认证，请先扫码登录 WhatsApp' });
    let { phone, filename, mime_type, file_data_base64, caption } = req.body;
    if (!phone || !filename || !file_data_base64) {
        return res.status(400).json({ status: 'failed', error: '缺少必填字段：phone, filename, file_data_base64' });
    }
    // Same robust phone cleaning as /send
    phone = sendResult.cleanPhone(phone);
    if (!phone) return res.status(400).json({ status: 'failed', error: '清理后电话号码为空' });
    const jid = phone.includes('@') ? phone : `${phone}@s.whatsapp.net`;
    let fileBuffer;
    try {
        fileBuffer = Buffer.from(file_data_base64, 'base64');
    } catch (e) {
        return res.status(400).json({ status: 'failed', error: '文件数据编码无效' });
    }
    const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB WhatsApp limit
    if (fileBuffer.length > MAX_FILE_SIZE) {
        return res.status(400).json({ status: 'failed', error: `文件过大（${(fileBuffer.length/1024/1024).toFixed(1)}MB），超过50MB限制` });
    }
    console.log('[send-document] sending', filename, `(${(fileBuffer.length/1024).toFixed(1)}KB)`, 'to', jid);
    try {
        const messageId = inbound.generateMessageId(crypto.randomBytes);
        ownSentIds.add(messageId);
        await Promise.race([
            sock.sendMessage(jid, {
                document: fileBuffer,
                mimetype: mime_type || 'application/pdf',
                fileName: filename
            }, { messageId }),
            new Promise((_, reject) => setTimeout(() => reject(new Error('send timeout')), 60000))
        ]);
        console.log('[send-document] done:', filename);
        res.json({ status: 'sent' });
    } catch (e) {
        let errorMsg = e.message || 'unknown';
        if (errorMsg === 'send timeout') {
            errorMsg = '文件发送超时（60秒）。文件可能过大或网络不稳定，请稍后重试。';
        }
        console.error('[send-document] failed:', errorMsg);
        res.json({ status: 'failed', error: errorMsg });
    }
});

['SIGINT', 'SIGTERM'].forEach(s => process.on(s, () => {
    isShutdown = true; resetBackoff();
    // Messages still waiting in the batch window are already persisted nowhere:
    // hand them to Python (stored, replies may be skipped) before exiting.
    const pendingQueues = Array.from(jidQueues.values());
    try { batcher.flushAll(); } catch (_) {}
    Promise.race([
        Promise.allSettled([...pendingQueues, ...Array.from(jidQueues.values())]),
        new Promise(resolve => setTimeout(resolve, 3000)),
    ]).finally(() => { if (sock) sock.end(undefined); process.exit(0); });
}));

async function loadBaileys() {
    const m = await import('@whiskeysockets/baileys');
    makeWASocket = m.makeWASocket;
    useMultiFileAuthState = m.useMultiFileAuthState;
    DisconnectReason = m.DisconnectReason;
}

app.post('/sync-contacts', async (_, r) => {
    if (!sock || !clientReady) return r.json({ ok: false, error: 'not connected' });
    const allContacts = Array.from(contacts.values());
    r.json({ ok: true, count: allContacts.length, contacts: allContacts });
});

(async () => {
    await loadBaileys();
    console.log(`Bridge listening on ${BRIDGE_HOST}:${BRIDGE_PORT}`);
    app.listen(BRIDGE_PORT, BRIDGE_HOST, start);
})();
