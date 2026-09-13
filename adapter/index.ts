/**
 * WhatsApp bridge (spec §3).
 *
 * Owns the Baileys session and nothing else: inbound messages are normalised
 * and pushed onto a Redis stream, outbound goes out over a small HTTP surface
 * that app/channel/baileys.py already calls. No business logic lives here, so
 * swapping to the Cloud API later replaces this process and one Python class.
 */
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  type WASocket,
  type proto,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import { createClient } from 'redis'

const PORT = Number(process.env.ADAPTER_PORT ?? 3001)
const REDIS_URL = process.env.REDIS_URL ?? 'redis://localhost:6379'
const STREAM = process.env.INBOUND_STREAM ?? 'inbound'
const SESSION_DIR = process.env.SESSION_DIR ?? './session'
const STREAM_MAXLEN = 10_000

const log = pino({ level: process.env.LOG_LEVEL ?? 'info' })
const redis = createClient({ url: REDIS_URL })

let sock: WASocket | null = null
let connected = false

/** WhatsApp caps a text message at 4096 chars. */
const MAX_CHARS = 4096

function jidToWaId(jid: string): string {
  return jid.split('@')[0].split(':')[0]
}

function textOf(msg: proto.IWebMessageInfo): string | null {
  const m = msg.message
  if (!m) return null
  return (
    m.conversation ??
    m.extendedTextMessage?.text ??
    m.imageMessage?.caption ??
    m.videoMessage?.caption ??
    m.buttonsResponseMessage?.selectedDisplayText ??
    m.listResponseMessage?.title ??
    null
  )
}

/**
 * Baileys button and list messages are unreliable on personal accounts — they
 * frequently do not render at all. Rendering them as numbered text keeps
 * disambiguation usable, which the instrument resolver depends on for
 * anything ambiguous ("gold", "chandi", "tata motors").
 */
function flatten(payload: OutboundPayload): string {
  const lines = [payload.text]
  if (payload.list_rows?.length) {
    payload.list_rows.forEach((row, i) => lines.push(`  ${i + 1}  ${row.label}`))
  } else if (payload.buttons?.length) {
    payload.buttons.forEach((b, i) => lines.push(`  ${i + 1}  ${b}`))
  }
  const out = lines.join('\n')
  return out.length > MAX_CHARS ? `${out.slice(0, MAX_CHARS - 1)}…` : out
}

interface OutboundPayload {
  wa_id: string
  kind: 'text' | 'image' | 'buttons' | 'list'
  text: string
  buttons?: string[]
  list_rows?: { id: string; label: string }[]
  image_b64?: string
  reply_to?: string | null
  idempotency_key?: string | null
}

async function start(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR)
  const { version } = await fetchLatestBaileysVersion()
  log.info({ version }, 'starting baileys')

  sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false,
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update

    if (qr) {
      console.log('\nScan this with the spare SIM\'s WhatsApp:\n')
      qrcode.generate(qr, { small: true })
    }

    if (connection === 'open') {
      connected = true
      log.info('connected')
    }

    if (connection === 'close') {
      connected = false
      const status = (lastDisconnect?.error as { output?: { statusCode?: number } })?.output
        ?.statusCode

      if (status === DisconnectReason.loggedOut) {
        // Exit non-zero so a dead session is loud rather than silently quiet.
        // Spec §23 runbook: "bot silent, no errors" is the Baileys failure.
        log.error('logged out — delete the session directory and re-scan the QR')
        process.exit(1)
      }

      log.warn({ status }, 'connection closed, reconnecting')
      setTimeout(() => void start(), 2_000)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return

    for (const msg of messages) {
      const jid = msg.key.remoteJid
      if (!jid || msg.key.fromMe) continue
      if (jid.endsWith('@g.us') || jid === 'status@broadcast') continue // no groups

      const text = textOf(msg)
      if (!text) continue

      const entry = {
        channel_msg_id: msg.key.id ?? '',
        wa_id: jidToWaId(jid),
        text,
        ts: String(Number(msg.messageTimestamp ?? 0) * 1000),
        quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId ?? '',
      }

      try {
        await redis.xAdd(STREAM, '*', entry, {
          TRIM: { strategy: 'MAXLEN', strategyModifier: '~', threshold: STREAM_MAXLEN },
        })
        log.info({ wa_id: entry.wa_id, id: entry.channel_msg_id }, 'inbound')
      } catch (err) {
        log.error({ err }, 'failed to publish inbound — message dropped')
      }
    }
  })
}

// ---- HTTP surface --------------------------------------------------

function readJson(req: IncomingMessage): Promise<OutboundPayload> {
  return new Promise((resolve, reject) => {
    let body = ''
    req.on('data', (chunk) => {
      body += chunk
      if (body.length > 8_000_000) reject(new Error('payload too large'))
    })
    req.on('end', () => {
      try {
        resolve(JSON.parse(body))
      } catch (err) {
        reject(err)
      }
    })
    req.on('error', reject)
  })
}

function send(res: ServerResponse, code: number, body: unknown): void {
  const json = JSON.stringify(body)
  res.writeHead(code, { 'content-type': 'application/json' })
  res.end(json)
}

const server = createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      return send(res, connected ? 200 : 503, { connected })
    }

    if (req.method === 'POST' && req.url === '/typing') {
      const { wa_id, on } = (await readJson(req)) as unknown as { wa_id: string; on: boolean }
      await sock?.sendPresenceUpdate(on ? 'composing' : 'paused', `${wa_id}@s.whatsapp.net`)
      return send(res, 200, { ok: true })
    }

    if (req.method === 'POST' && req.url === '/send') {
      if (!sock || !connected) return send(res, 503, { error: 'not connected' })

      const payload = await readJson(req)
      const jid = `${payload.wa_id}@s.whatsapp.net`

      const content =
        payload.kind === 'image' && payload.image_b64
          ? { image: Buffer.from(payload.image_b64, 'base64'), caption: payload.text }
          : { text: flatten(payload) }

      const sent = await sock.sendMessage(jid, content as never)
      const id = sent?.key?.id
      if (!id) {
        // Never report delivery without an id from the channel (spec §18.2).
        return send(res, 502, { error: 'send returned no message id' })
      }
      log.info({ wa_id: payload.wa_id, id }, 'outbound')
      return send(res, 200, { channel_msg_id: id })
    }

    send(res, 404, { error: 'not found' })
  } catch (err) {
    log.error({ err }, 'request failed')
    send(res, 500, { error: String(err) })
  }
})

async function main(): Promise<void> {
  redis.on('error', (err) => log.error({ err }, 'redis'))
  await redis.connect()

  // Listen before connecting to WhatsApp, so /health answers 503 while the
  // session is still pairing or broken. Starting Baileys first means a
  // connection failure leaves nothing to ask.
  server.listen(PORT, () => log.info({ port: PORT }, 'adapter listening'))

  try {
    await start()
  } catch (err) {
    log.error({ err }, 'baileys failed to start — /health will report disconnected')
  }
}

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    log.info('shutting down')
    server.close()
    void redis.quit().finally(() => process.exit(0))
  })
}

void main()
