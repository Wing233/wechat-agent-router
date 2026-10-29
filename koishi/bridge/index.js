'use strict'

/**
 * Koishi -> WeChat Agent Router bridge.
 *
 * Koishi owns platform adapters (QQ/OneBot, Telegram, Discord, ...).
 * This plugin only forwards a normalized message and sends the reply back.
 */

module.exports.name = 'router-bridge'

module.exports.apply = (ctx, config = {}) => {
  const routerUrl = String(config.routerUrl || process.env.ROUTER_URL || 'http://127.0.0.1:9900')
    .replace(/\/+$/, '')
  const token = String(config.token || process.env.ROUTER_CHANNEL_TOKEN || '')
  const channel = String(config.channel || 'koishi')
  const timeoutMs = Number(config.timeoutMs || 90000)
  const logger = ctx.logger('router-bridge')

  if (!token) {
    logger.warn('ROUTER_CHANNEL_TOKEN is empty; messages will be rejected by Router')
  }

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
