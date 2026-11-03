'use strict'

/**
 * Koishi -> WeChat Agent Router bridge.
 *
 * Koishi owns platform adapters (QQ/OneBot, Telegram, Discord, ...).
 * This plugin only forwards a normalized message and sends the reply back.
 */

const fs = require('node:fs')
const path = require('node:path')
const { h } = require('koishi')

module.exports.name = 'router-bridge'

module.exports.apply = (ctx, config = {}) => {
  const routerUrl = String(config.routerUrl || process.env.ROUTER_URL || 'http://127.0.0.1:9900')
    .replace(/\/+$/, '')
  const token = String(config.token || process.env.ROUTER_CHANNEL_TOKEN || '')
  const channel = String(config.channel || 'koishi')
  const timeoutMs = Number(config.timeoutMs || 90000)
  const outboundPollMs = Math.max(1000, Number(config.outboundPollMs || 2000))
  const logger = ctx.logger('router-bridge')

  // QQ only accepts SILK audio for voice messages; decode the mp3 we generate
  // and re-encode with silk-wasm so the adapter can send it as-is.
  async function audioElement(file) {
    const { MPEGDecoder } = require('mpg123-decoder')
    const silk = require('silk-wasm')
    const mp3 = fs.readFileSync(file)
    const decoder = new MPEGDecoder()
    await decoder.ready
    try {
      const decoded = decoder.decode(new Uint8Array(mp3))
      const samples = decoded.samplesDecoded
      const sampleRate = decoded.sampleRate
      const channel = decoded.channelData[0] || new Float32Array(0)
      const pcm = Buffer.alloc(samples * 2)
      for (let i = 0; i < samples; i++) {
        let value = channel[i] || 0
        value = Math.max(-1, Math.min(1, value))
        pcm.writeInt16LE(Math.round(value * 32767), i * 2)
      }
      const encoded = await silk.encode(new Uint8Array(pcm), sampleRate)
      return h('audio', {
        src: `data:audio/silk;base64,${Buffer.from(encoded.data).toString('base64')}`,
      })
    } finally {
      decoder.free?.()
    }
  }

  if (!token) {
    logger.warn('ROUTER_CHANNEL_TOKEN is empty; messages will be rejected by Router')
  }

  let outboundTimer = null
  let outboundBusy = false

  async function ackOutbound(ids, ok, error = '') {
    if (!ids.length) return
    try {
      await fetch(`${routerUrl}/channel/outbound/ack`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Channel-Token': token,
        },
        body: JSON.stringify({ channel, ids, ok, error }),
      })
    } catch (failure) {
      logger.warn('outbound ack failed: %s', failure?.message || failure)
    }
  }

  async function pollOutbound() {
    if (outboundBusy || !token) return
    outboundBusy = true
    try {
      const response = await fetch(
        `${routerUrl}/channel/outbound?channel=${encodeURIComponent(channel)}&limit=10`,
        { headers: { 'X-Channel-Token': token } },
      )
      if (!response.ok) {
        logger.warn('outbound poll HTTP %s', response.status)
        return
      }
      const data = await response.json().catch(() => ({}))
      const messages = Array.isArray(data.messages) ? data.messages : []
      for (const message of messages) {
        const bot = pickBot(ctx, message.platform)
        if (!bot) {
          logger.warn('outbound %s waiting: no bot for platform %s',
            message.id, message.platform || 'unknown')
          continue
        }
        const target = stripPlatform(message.chat_id, message.platform)
        try {
          let content = String(message.text || '')
          if (message.media_type === 'audio' && message.media_path) {
            const dataDir = String(config.dataDir || '/opt/wechat-agent-router/data')
            const file = path.join(dataDir, String(message.media_path))
            content = await audioElement(file)
          }
          await bot.sendMessage(target, content)
          await ackOutbound([message.id], true)
          logger.info('outbound %s -> %s', message.id, target)
        } catch (failure) {
          await ackOutbound([message.id], false,
            failure?.message || 'send failed')
          logger.warn('outbound %s failed: %s', message.id,
            failure?.message || failure)
        }
      }
    } catch (failure) {
      logger.warn('outbound poll failed: %s', failure?.message || failure)
    } finally {
      outboundBusy = false
    }
  }

  outboundTimer = setInterval(pollOutbound, outboundPollMs)
  if (outboundTimer && typeof outboundTimer.unref === 'function') outboundTimer.unref()
  ctx.on('dispose', () => {
    if (outboundTimer) clearInterval(outboundTimer)
    outboundTimer = null
  })

  ctx.on('message', async (session) => {
    if (!session || session.userId === session.selfId) return
    if (session.author && session.author.isBot) return

    const text = extractText(session)
    if (!text) return

    const platform = String(session.platform || 'unknown')
    const chatId = String(session.channelId || session.userId || '')
    const userId = String(session.userId || '')
    if (!chatId || !userId) return

    const payload = {
      channel,
      platform,
      account_id: String(session.selfId || ''),
      chat_id: `${platform}:${chatId}`,
      chat_type: session.isDirect ? 'private' : 'group',
      user_id: `${platform}:${userId}`,
      user_name: session.author?.name || session.username || '',
      text,
      message_id: String(session.messageId || ''),
      is_mention: isMentioned(session),
      raw: {
        guild_id: session.guildId || null,
      },
    }

    logger.info('%s %s -> %s', platform, payload.user_id, text.slice(0, 80))
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await fetch(`${routerUrl}/channel/inbound`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Channel-Token': token,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        logger.warn('Router HTTP %s: %s', response.status, data.detail || '')
        if (response.status === 401 || response.status === 403) {
          await session.send('渠道网关配置有问题，请联系管理员。')
        } else {
          await session.send(`Router 暂时不可用：${data.detail || response.status}`)
        }
        return
      }
      if (data.ignored) return
      if (data.reply) await session.send(String(data.reply))
      if (data.voice_path) {
        try {
          await session.send(await audioElement(String(data.voice_path)))
        } catch (failure) {
          logger.warn('voice reply failed: %s', failure?.message || failure)
        }
      }
    } catch (error) {
      logger.warn('Router request failed: %s', error?.message || error)
      await session.send('Router 暂时连不上，请稍后再试。')
    } finally {
      clearTimeout(timer)
    }
  })
}

function extractText(session) {
  const elements = Array.isArray(session.elements) ? session.elements : []
  const text = elements
    .filter((element) => element && element.type === 'text')
    .map((element) => element.attrs?.content || '')
    .join('')
  return String(text || session.content || '').trim()
}

function isMentioned(session) {
  if (session.isDirect) return true
  const selfId = String(session.selfId || '')
  const elements = Array.isArray(session.elements) ? session.elements : []
  if (elements.some((element) => element && element.type === 'at' &&
    String(element.attrs?.id || '') === selfId)) return true
  const username = String(session.username || '')
  return Boolean(username && String(session.content || '').includes('@' + username))
}

function pickBot(ctx, platform) {
  const bots = Array.isArray(ctx.bots) ? ctx.bots : []
  const wanted = String(platform || '')
  if (wanted) {
    const exact = bots.find((bot) => String(bot.platform || '') === wanted)
    if (exact) return exact
  }
  return bots[0] || null
}

function stripPlatform(chatId, platform) {
  const text = String(chatId || '')
  const prefix = platform ? `${platform}:` : ''
  return prefix && text.startsWith(prefix) ? text.slice(prefix.length) : text
}
