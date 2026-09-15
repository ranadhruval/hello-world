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
} from 'baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import { createClient } from 'redis'

const PORT = Number(process.env.ADAPTER_PORT ?? 3001)
// Bind loopback only. /send can make this WhatsApp account message anyone, so
// listening on 0.0.0.0 would hand that to everything on the same network.
// The worker is the only caller and runs on the same box.
const HOST = process.env.ADAPTER_HOST ?? '127.0.0.1'
const REDIS_URL = process.env.REDIS_URL ?? 'redis://localhost:6379'
const STREAM = process.env.INBOUND_STREAM ?? 'inbound'
const SESSION_DIR = process.env.SESSION_DIR ?? './session'
const STREAM_MAXLEN = 10_000
// Where a reply is addressed. 'echo' answers on the exact JID the message
// arrived on, which is where WhatsApp established the encryption session with
// this contact. 'phone' resolves a @lid chat to the phone number behind it.
// Echo is the default because addressing the same person by two identities
// makes libsignal tear down and rebuild the session -- the "Closing open
// session in favor of incoming prekey bundle" line -- and a message encrypted
// against the losing session is accepted here and never renders there.
const REPLY_TO = process.env.REPLY_TO === 'phone' ? 'phone' : 'echo'
// Baileys' own logger was silent, which meant every failure inside the
// library -- a session it could not build, a prekey fetch that failed, a send
// it gave up on -- happened with nothing written anywhere. A send can return a
// message id and never be transmitted, and we spent a day unable to see why.
// 'warn' is quiet in normal running; set BAILEYS_LOG_LEVEL=debug to see the
// protocol itself.
const BAILEYS_LOG_LEVEL = process.env.BAILEYS_LOG_LEVEL ?? 'warn'

const log = pino({ level: process.env.LOG_LEVEL ?? 'info' })
const redis = createClient({ url: REDIS_URL })

let sock: WASocket | null = null
let connected = false

// Every socket carries the generation it was built in. A socket that is no
// longer current ignores its own events, because the alternative is what the
// logs showed: a closed socket's handler schedules a restart, the restart
// builds a second socket without retiring the first, and each close from
// either one schedules another. WhatsApp reads overlapping sessions on one
// set of credentials as a conflict, which is how a reconnect becomes a
// logout and how sends stop being delivered while still returning ids.
let generation = 0
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let backoffMs = 2_000
const BACKOFF_MIN_MS = 2_000
const BACKOFF_MAX_MS = 60_000

function teardown(previous: WASocket | null): void {
  if (!previous) return
  try {
    previous.ev.removeAllListeners('creds.update')
    previous.ev.removeAllListeners('connection.update')
    previous.ev.removeAllListeners('messages.upsert')
    previous.end(undefined)
  } catch (err) {
    log.warn({ err }, 'could not retire the previous socket cleanly')
  }
}

/** At most one reconnect in flight, backing off so a bad spell does not storm. */
function scheduleReconnect(): void {
  if (reconnectTimer) return
  const delay = backoffMs
  backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS)
  log.warn({ delay }, 'reconnecting')
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    void start()
  }, delay)
}

/** WhatsApp caps a text message at 4096 chars. */
const MAX_CHARS = 4096

// sendMessage resolves once the message is handed to the socket, so the id it
// returns proves composition and nothing else. These come back later, from
// WhatsApp: SERVER_ACK means the servers took it, DELIVERY_ACK means it
// reached the other phone. A send with a message id and no SERVER_ACK never
// actually left this machine, which is the difference between our bug and
// theirs -- and it is invisible without this.
const STATUS_NAMES: Record<number, string> = {
  0: 'ERROR',
  1: 'PENDING',
  2: 'SERVER_ACK',
  3: 'DELIVERY_ACK',
  4: 'READ',
  5: 'PLAYED',
}

function jidToWaId(jid: string): string {
  return jid.split('@')[0].split(':')[0]
}

/**
 * WhatsApp is moving chats onto LID addressing, where the chat JID is
 * <opaque>@lid and its digits are not a phone number. Encrypting to a bare
 * @lid address does not reliably establish a session: sendMessage returns a
 * message id, the send is accepted, and nothing is ever delivered. That is the
 * worst failure this process has, because a bot that answers into the void is
 * indistinguishable from a bot with nothing to say.
 *
 * Baileys 7 tracks this itself and stamps outgoing messages with the right
 * addressing mode, so replies echo the address the message arrived on and the
 * mapping is only a fallback for a proactive send with no inbound to echo.
 * key.remoteJidAlt carries the other identity of the same person: the phone
 * JID when the chat is LID-addressed.
 */
const LID_MAP = 'lid_pn'

async function rememberLid(jid: string, alt?: string | null): Promise<void> {
  if (!alt || !jid.endsWith('@lid') || alt.endsWith('@lid')) return
  try {
    await redis.hSet(LID_MAP, jidToWaId(jid), alt)
  } catch (err) {
    log.warn({ err }, 'could not record the lid mapping')
  }
}

/** The address to actually send to. See REPLY_TO. */
async function deliverableJid(jid: string): Promise<string> {
  if (REPLY_TO === 'echo') return jid
  if (!jid.endsWith('@lid')) return jid
  try {
    const pn = await redis.hGet(LID_MAP, jidToWaId(jid))
    if (pn) return pn
  } catch (err) {
    log.warn({ err }, 'lid lookup failed')
  }
  log.warn({ jid }, 'no phone address known for this lid — sending to the lid may not deliver')
  return jid
}

/**
 * The inverse of the worker's canonical_wa_id, for a send with no inbound
 * message to echo. A LID identity rebuilt as <digits>@s.whatsapp.net is a
 * different account, or nobody at all.
 */
function waIdToJid(waId: string): string {
  return waId.startsWith('lid:') ? `${waId.slice(4)}@lid` : `${waId}@s.whatsapp.net`
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
  jid?: string
  kind: 'text' | 'image' | 'buttons' | 'list'
  text: string
  buttons?: string[]
  list_rows?: { id: string; label: string }[]
  image_b64?: string
  reply_to?: string | null
  idempotency_key?: string | null
}

async function start(): Promise<void> {
  const gen = ++generation
  teardown(sock)
  sock = null
  connected = false

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR)
  const { version } = await fetchLatestBaileysVersion()
  log.info({ version, gen, replyTo: REPLY_TO, baileysLog: BAILEYS_LOG_LEVEL }, 'starting baileys')

  const current = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: BAILEYS_LOG_LEVEL }),
    markOnlineOnConnect: false,
  })
  sock = current

  current.ev.on('creds.update', saveCreds)

  current.ev.on('connection.update', (update) => {
    if (gen !== generation) return // a retired socket, talking to nobody
    const { connection, lastDisconnect, qr } = update

    if (qr) {
      console.log('\nScan this with the spare SIM\'s WhatsApp:\n')
      qrcode.generate(qr, { small: true })
    }

    if (connection === 'open') {
      connected = true
      backoffMs = BACKOFF_MIN_MS
      log.info({ jid: current.user?.id }, 'connected')
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

      log.warn({ status }, 'connection closed')
      scheduleReconnect()
    }
  })

  current.ev.on('messages.update', (updates) => {
    if (gen !== generation) return
    for (const u of updates) {
      if (!u.key?.fromMe) continue
      const status = u.update?.status
      if (status === undefined || status === null) continue
      const name = STATUS_NAMES[status] ?? String(status)
      const line = { id: u.key.id, jid: u.key.remoteJid, status: name }
      if (status === 0) log.error(line, 'receipt')
      else log.info(line, 'receipt')
    }
  })

  current.ev.on('messages.upsert', async ({ messages, type }) => {
    if (gen !== generation) return
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
        // The exact address to reply to. wa_id is the user-facing identity and
        // cannot be turned back into a JID: WhatsApp also addresses chats as
        // <id>@lid, where the digits are an opaque id and not a phone number.
        jid,
        text,
        ts: String(Number(msg.messageTimestamp ?? 0) * 1000),
        quoted_id: msg.message?.extendedTextMessage?.contextInfo?.stanzaId ?? '',
      }

      await rememberLid(jid, msg.key.remoteJidAlt)

      try {
        await redis.xAdd(STREAM, '*', entry, {
          TRIM: { strategy: 'MAXLEN', strategyModifier: '~', threshold: STREAM_MAXLEN },
        })
        log.info({ jid, wa_id: entry.wa_id, id: entry.channel_msg_id }, 'inbound')
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
      // Report our own JID: it shows which addressing scheme this account is
      // on, which is the one thing wa_id alone cannot tell you.
      return send(res, connected ? 200 : 503, {
        connected,
        jid: sock?.user?.id ?? null,
      })
    }

    if (req.method === 'POST' && req.url === '/typing') {
      const body = (await readJson(req)) as unknown as {
        wa_id: string
        jid?: string
        on: boolean
      }
      const target = await deliverableJid(body.jid || waIdToJid(body.wa_id))
      await sock?.sendPresenceUpdate(body.on ? 'composing' : 'paused', target)
      return send(res, 200, { ok: true })
    }

    if (req.method === 'POST' && req.url === '/send') {
      if (!sock || !connected) return send(res, 503, { error: 'not connected' })

      const payload = await readJson(req)
      // Echo the address the message came from. Only fall back to building
      // one when there is nothing to echo — the console REPL, or a future
      // proactive send with no inbound message behind it.
      const jid = await deliverableJid(payload.jid || waIdToJid(payload.wa_id))

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
      log.info({ jid, wa_id: payload.wa_id, id }, 'outbound')
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
  server.listen(PORT, HOST, () => log.info({ host: HOST, port: PORT }, 'adapter listening'))

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
