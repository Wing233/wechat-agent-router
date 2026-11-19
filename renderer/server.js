'use strict'

const http = require('node:http')
const { chromium } = require('playwright-core')
const { PDFDocument } = require('pdf-lib')

const PORT = Number(process.env.PORT || 9801)
const TOKEN = String(process.env.RENDER_TOKEN || '')
const ALLOWED = String(process.env.ALLOWED_ORIGINS || 'https://lyph-nginx-1:8443,http://web:3000')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean)

let browserPromise = null
let busy = false

async function browser() {
  if (!browserPromise) {
    browserPromise = chromium.launch({
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-software-rasterizer',
        '--js-flags=--max-old-space-size=128',
      ],
    })
  }
  return browserPromise
}

function allowedUrl(raw) {
  let url
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  if (!['http:', 'https:'].includes(url.protocol)) return null
  if (!url.pathname.startsWith('/print/resume/')) return null
  const origin = `${url.protocol}//${url.host}`
  if (!ALLOWED.includes(origin)) return null
  return url
}

function json(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload))
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  })
  res.end(body)
}

async function readBody(req, limit = 64 * 1024) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > limit) throw new Error('body too large')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

async function render(payload) {
  const url = allowedUrl(String(payload.url || ''))
  if (!url) throw Object.assign(new Error('url not allowed'), { status: 403 })
  const target = url.toString()
  const timeout = Math.max(5000, Math.min(60000, Number(payload.timeout_ms || 45000)))
  const instance = await browser()
  const context = await instance.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 794, height: 1122 },
  })
  const page = await context.newPage()
  try {
    await page.route('**/*', (route) => {
      const requestUrl = route.request().url()
      if (requestUrl.startsWith('data:') || requestUrl.startsWith('blob:')) {
        return route.continue()
      }
      try {
        const parsed = new URL(requestUrl)
        const origin = `${parsed.protocol}//${parsed.host}`
        if (ALLOWED.includes(origin)) return route.continue()
      } catch {}
      return route.abort()
    })
    const response = await page.goto(target, { waitUntil: 'networkidle', timeout })
    if (!response || response.status() >= 400) {
      throw Object.assign(new Error(`print page HTTP ${response ? response.status() : 'no response'}`),
        { status: 502 })
    }
    await page.evaluate(() => document.fonts.ready)
    const height = await page.evaluate(() => {
      const el = document.querySelector('.resume-print-only .resume-paper-inner > div')
      return el ? Math.max(el.scrollHeight, el.offsetHeight) : 0
    })
    if (height > 1130) {
      return { overflow: true, height }
    }
    const pdf = await page.pdf({
      format: 'A4',
      printBackground: true,
      preferCSSPageSize: true,
      displayHeaderFooter: false,
      margin: { top: '0', right: '0', bottom: '0', left: '0' },
    })
    const doc = await PDFDocument.load(pdf)
    return {
      overflow: false,
      height,
      pages: doc.getPageCount(),
      bytes: pdf.length,
      pdf_base64: pdf.toString('base64'),
    }
  } finally {
    await page.close().catch(() => {})
    await context.close().catch(() => {})
  }
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    return json(res, 200, { ok: true, busy })
  }
  if (req.method !== 'POST' || req.url !== '/render') {
    return json(res, 404, { error: 'not found' })
  }
  if (!TOKEN || req.headers['x-render-token'] !== TOKEN) {
    return json(res, 401, { error: 'invalid render token' })
  }
  if (busy) {
    return json(res, 429, { error: 'renderer busy' })
  }
  busy = true
  try {
    const body = await readBody(req)
    const payload = JSON.parse(body || '{}')
    const result = await render(payload)
    if (result.overflow) {
      return json(res, 422, { error: 'overflow', height: result.height })
    }
    return json(res, 200, result)
  } catch (error) {
    const status = Number(error.status || 500)
    console.error('render failed:', error.message)
    return json(res, status, { error: error.message || 'render failed' })
  } finally {
    busy = false
  }
})

server.listen(PORT, '0.0.0.0', () => {
  console.log(`resume-render listening on ${PORT}`)
})

function shutdown() {
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(0), 3000).unref()
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
