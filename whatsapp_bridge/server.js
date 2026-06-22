const express = require('express');
const fs = require('fs');
const path = require('path');
const qrcode = require('qrcode');
const pino = require('pino');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
  Browsers,
} = require('@whiskeysockets/baileys');

const PORT = Number(process.env.PORT || 8080);
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, '.auth');
const INSTANCE_NAME = process.env.WHATSAPP_INSTANCE_NAME || 'local-whatsapp';
const LOG_FILE = path.join(__dirname, 'bridge.log');

fs.mkdirSync(AUTH_DIR, { recursive: true });

function logLine(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`;
  try {
    fs.appendFileSync(LOG_FILE, line, 'utf8');
  } catch {}
  try {
    console.log(message);
  } catch {}
}

process.on('uncaughtException', (error) => {
  logLine(`uncaughtException: ${error?.stack || error}`);
});

process.on('unhandledRejection', (error) => {
  logLine(`unhandledRejection: ${error?.stack || error}`);
});

const app = express();
app.use(express.json({ limit: '2mb' }));

let sock = null;
let bootPromise = null;
let connectionState = 'closed';
let latestQrText = '';
let latestQrDataUrl = '';
let lastError = '';
let lastConnectedAt = null;
let lastUpdateAt = null;
let displayName = '';

function loggedIn() {
  return connectionState === 'open';
}

async function clearQr() {
  latestQrText = '';
  latestQrDataUrl = '';
}

async function ensureSocket() {
  if (sock && (connectionState === 'open' || connectionState === 'connecting' || connectionState === 'qr')) {
    return sock;
  }
  if (bootPromise) {
    return bootPromise;
  }
  bootPromise = (async () => {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();
    const socket = makeWASocket({
      version,
      auth: state,
      printQRInTerminal: false,
      browser: Browsers.macOS('Desktop'),
      logger: pino({ level: 'silent' }),
    });
    sock = socket;
    connectionState = 'connecting';
    lastError = '';

    socket.ev.on('creds.update', saveCreds);
    socket.ev.on('connection.update', async (update) => {
      lastUpdateAt = new Date().toISOString();
      logLine(`connection.update ${JSON.stringify({
        connection: update.connection || null,
        hasQr: Boolean(update.qr),
        lastDisconnect: update.lastDisconnect?.error?.output?.statusCode || null,
      })}`);
      if (update.qr) {
        latestQrText = update.qr;
        latestQrDataUrl = await qrcode.toDataURL(update.qr);
        connectionState = 'qr';
      }
      if (update.connection === 'open') {
        connectionState = 'open';
        lastConnectedAt = new Date().toISOString();
        lastError = '';
        displayName = socket.user?.name || displayName || INSTANCE_NAME;
        await clearQr();
      }
      if (update.connection === 'close') {
        connectionState = 'closed';
        const reason = update.lastDisconnect?.error?.output?.statusCode;
        lastError = reason ? `disconnect:${reason}` : 'closed';
        if (reason === DisconnectReason.loggedOut) {
          try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
        }
        sock = null;
        bootPromise = null;
      }
    });
    return socket;
  })();
  try {
    return await bootPromise;
  } finally {
    if (connectionState !== 'connecting') {
      bootPromise = null;
    }
  }
}

function normalizeNumber(number) {
  const digits = String(number || '').replace(/\D/g, '');
  return digits;
}

async function statusPayload() {
  return {
    data: {
      Connected: loggedIn(),
      LoggedIn: loggedIn(),
      Name: displayName || INSTANCE_NAME,
    },
    message: 'success',
  };
}

app.get('/health', async (_req, res) => {
  res.json({ ok: true, state: connectionState, lastError, lastConnectedAt, lastUpdateAt });
});

app.get('/instance/status', async (_req, res) => {
  res.json(await statusPayload());
});

app.get('/instance/qr', async (_req, res) => {
  await ensureSocket();
  if (latestQrDataUrl) {
    return res.json({ data: { Qrcode: latestQrDataUrl, Code: latestQrText }, message: 'success' });
  }
  if (connectionState === 'open') {
    return res.status(404).json({ message: 'instance already connected' });
  }
  return res.status(404).json({ message: 'qr not available yet', state: connectionState, lastError });
});

app.post('/instance/connect', async (_req, res) => {
  await ensureSocket();
  if (connectionState === 'open') {
    return res.json({ data: { connected: true, name: displayName || INSTANCE_NAME }, message: 'success' });
  }
  if (latestQrText || latestQrDataUrl) {
    return res.json({ data: { eventString: 'QRCODE', name: displayName || INSTANCE_NAME }, message: 'success' });
  }
  return res.json({ data: { eventString: 'CONNECTING', name: displayName || INSTANCE_NAME }, message: 'success' });
});

app.post('/instance/pair', async (req, res) => {
  await ensureSocket();
  const phone = normalizeNumber(req.body?.phone);
  if (!phone) {
    return res.status(400).json({ message: 'phone required' });
  }
  if (!sock?.requestPairingCode) {
    return res.status(501).json({ message: 'pairing code not supported by this build' });
  }
  try {
    const code = await sock.requestPairingCode(phone);
    return res.json({ data: { PairingCode: code }, message: 'success' });
  } catch (error) {
    lastError = String(error?.message || error);
    return res.status(500).json({ message: 'pairing failed', error: lastError });
  }
});

app.post(['/send/text', '/message/sendText'], async (req, res) => {
  await ensureSocket();
  if (!loggedIn()) {
    return res.status(409).json({ message: 'instance not connected', state: connectionState, lastError });
  }
  const number = normalizeNumber(req.body?.number);
  const text = String(req.body?.text || '');
  if (!number || !text) {
    return res.status(400).json({ message: 'number and text are required' });
  }
  const jid = number.includes('@') ? number : `${number}@s.whatsapp.net`;
  try {
    const sent = await sock.sendMessage(jid, { text });
    return res.json({ data: { jid, messageId: sent?.key?.id || null }, message: 'success' });
  } catch (error) {
    lastError = String(error?.message || error);
    return res.status(500).json({ message: 'send failed', error: lastError });
  }
});

app.get('/instance/me', async (_req, res) => {
  await ensureSocket();
  res.json({ data: { name: displayName || INSTANCE_NAME, connected: loggedIn(), state: connectionState }, message: 'success' });
});

app.listen(PORT, '127.0.0.1', () => {
  logLine(`Local WhatsApp bridge listening on http://127.0.0.1:${PORT}`);
});
