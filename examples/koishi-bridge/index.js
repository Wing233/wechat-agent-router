/**
 * Koishi → WeChat Agent Router bridge.
 *
 * Drop this file into your Koishi plugins directory and add it to koishi.yml:
 *
 *   plugins:
 *     ./plugins/router-bridge:
 *       routerUrl: http://127.0.0.1:9900
 *       channel: qq
 *       secret: <same secret as config.json channels.qq.secret>
 *       platform: qq
 *       requireAt: true
 *
 * The bridge normalizes Koishi messages into Router envelopes and polls the
 * Router outbox for replies. It never talks to the WeChat hub directly.
 */
const crypto = require("node:crypto");

const DEFAULTS = {
  routerUrl: "http://127.0.0.1:9900",
  channel: "qq",
  secret: "",
  platform: "qq",
  pollInterval: 2000,
  requireAt: true,
  enabled: true,
  textLimit: 4000,
  groupAllowlist: [],
  accountId: "",
};

function sign(secret, body) {
  return crypto.createHmac("sha256", secret).update(body).digest("hex");
}

function normalize(config, session) {
  const stripped = session.stripped || {};
  const text = String(stripped.content || session.content || "").trim();
  const attachments = [];
  for (const element of session.elements || []) {
    if (element.type === "img" || element.type === "image") {
      const attrs = element.attrs || {};
      const url = attrs.src || attrs.url || "";
      if (url) attachments.push({ type: "image", url, name: attrs.file || "" });
    } else if (element.type === "file") {
      const attrs = element.attrs || {};
      const url = attrs.src || attrs.url || "";
      if (url) attachments.push({ type: "file", url, name: attrs.name || attrs.file || "" });
    }
  }
  const isDirect = Boolean(session.isDirect);
  const endpointId = isDirect
    ? `${config.channel}:private:${session.userId}`
    : `${config.channel}:group:${session.channelId}`;
  return {
    channel: config.channel,
    adapter: "koishi",
    account_id: `${session.platform}:${session.selfId}`,
    endpoint_id: endpointId,
    conversation_type: isDirect ? "private" : "group",
    conversation_id: String(session.channelId || session.userId || ""),
    message_id: String(session.messageId || ""),
    sender_id: String(session.userId || ""),
    sender_name: String(session.author?.name || session.username || session.userId || ""),
    text: text.slice(0, config.textLimit),
    attachments,
    mentions: [],
    raw: {
      platform: session.platform,
      guild_id: session.guildId || "",
      channel_id: session.channelId || "",
      user_id: session.userId || "",
      at_self: Boolean(stripped.atSelf),
    },
  };
}

async function postInbound(config, payload) {
  const body = JSON.stringify(payload);
  const response = await fetch(`${config.routerUrl.replace(/\/+$/, "")}/v1/channels/inbound`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-router-signature": sign(config.secret, body),
    },
    body,
  });
  if (!response.ok) {
    throw new Error(`router inbound ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

function pickBot(ctx, config, payload) {
  const bots = ctx.bots.filter((bot) => bot.platform === config.platform);
  if (payload?.account_id) {
    const exact = bots.find((bot) => `${bot.platform}:${bot.selfId}` === payload.account_id);
    if (exact) return exact;
  }
  if (config.accountId) {
    const exact = bots.find((bot) => `${bot.platform}:${bot.selfId}` === config.accountId);
    if (exact) return exact;
  }
  return bots[0];
}

async function deliver(ctx, config, item) {
  const payload = item.payload || {};
  const parts = String(item.endpoint_id || "").split(":");
  const kind = parts[1] || "";
  const target = parts.slice(2).join(":");
  if (!target) throw new Error(`bad endpoint: ${item.endpoint_id}`);
  const bot = pickBot(ctx, config, payload);
  if (!bot) throw new Error(`no online ${config.platform} bot`);
  let text = String(payload.text || "");
  if (payload.kind === "image" || payload.kind === "file") {
    text = text || payload.path || "";
    if (!payload.url && payload.path) {
      text += "\n（附件在 Router 服务器上，QQ 侧未同步）";
    }
  }
  if (!text) return;
  if (kind === "private") {
    if (typeof bot.sendPrivateMessage === "function") {
      await bot.sendPrivateMessage(target, text);
    } else {
      await bot.sendMessage(target, text);
    }
  } else {
    await bot.sendMessage(target, text);
  }
}

async function pollOnce(ctx, config, logger) {
  const base = config.routerUrl.replace(/\/+$/, "");
  const response = await fetch(
    `${base}/v1/channels/outbound?channel=${encodeURIComponent(config.channel)}&limit=20`,
    { headers: { "x-channel-secret": config.secret } },
  );
  if (!response.ok) {
    throw new Error(`router outbound ${response.status}: ${await response.text()}`);
  }
  const data = await response.json();
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) return;
  const delivered = [];
  for (const item of items) {
    try {
      await deliver(ctx, config, item);
      delivered.push(item.id);
    } catch (error) {
      logger.warn("deliver %s failed: %s", item.id, error.message || error);
    }
  }
  if (!delivered.length) return;
  const ack = await fetch(`${base}/v1/channels/outbound/ack`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-channel-secret": config.secret,
    },
    body: JSON.stringify({ channel: config.channel, ids: delivered }),
  });
  if (!ack.ok) {
    logger.warn("ack failed: %s", await ack.text());
  }
}

module.exports = {
  name: "router-bridge",
  apply(ctx, options) {
    const config = { ...DEFAULTS, ...options };
    const logger = ctx.logger("router-bridge");
    if (!config.secret) {
      logger.error("channels.%s.secret is empty; bridge disabled", config.channel);
      return;
    }
    const seen = new Map();

    ctx.on("message", async (session) => {
      if (!config.enabled) return;
      if (session.userId === session.selfId) return;
      const isDirect = Boolean(session.isDirect);
      if (!isDirect) {
        if (config.groupAllowlist.length && !config.groupAllowlist.includes(session.channelId)) {
          return;
        }
        const stripped = session.stripped || {};
        if (config.requireAt && !stripped.atSelf) return;
      }
      const messageId = String(session.messageId || "");
      if (messageId) {
        const now = Date.now();
        for (const [key, stamp] of seen) {
          if (now - stamp > 60000) seen.delete(key);
        }
        if (seen.has(messageId)) return;
        seen.set(messageId, now);
      }
      try {
        const payload = normalize(config, session);
        if (!payload.text && !payload.attachments.length) return;
        const result = await postInbound(config, payload);
        logger.debug("accepted %s -> %s", messageId, result.endpoint_id);
      } catch (error) {
        logger.warn("inbound failed: %s", error.message || error);
      }
    });

    let running = false;
    const timer = setInterval(async () => {
      if (running || !config.enabled) return;
      running = true;
      try {
        await pollOnce(ctx, config, logger);
      } catch (error) {
        logger.warn("poll failed: %s", error.message || error);
      } finally {
        running = false;
      }
    }, Math.max(500, config.pollInterval));

    ctx.on("dispose", () => clearInterval(timer));
  },
};
