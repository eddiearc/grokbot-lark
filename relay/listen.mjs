#!/usr/bin/env node
// Receives im.message.receive_v1 over the official long connection (node-sdk WSClient) and
// prints one JSON object per message on stdout, in the shape `lark-cli event consume` prints.
//
// Why not `lark-cli event consume`: its connection can go dead without an error and it never
// notices. Here the SDK closes and reopens the connection, inside this process, when no frame
// arrives within PING_TIMEOUT_SEC of a ping.
//
// The app ID and secret are the ones lark-cli stored for this bot. The secret is never printed.
//
//   node listen.mjs           run until stdin closes or TERM
//   node listen.mjs --check   exit 0 when the SDK and the credentials are usable, else 78
import { execFileSync } from "node:child_process";
import { createDecipheriv } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const EVENT = "im.message.receive_v1";
const PING_TIMEOUT_SEC = 30;
const HANDSHAKE_TIMEOUT_MS = 15_000;
const UNUSABLE = 78;

const note = (text) => process.stderr.write(`${text}\n`);

function fail(text) {
  note(`[listen] ${text}`);
  process.exit(UNUSABLE);
}

function configDir() {
  return process.env.LARKSUITE_CLI_CONFIG_DIR || join(homedir(), ".lark-cli");
}

/** Directories where lark-cli keeps its encrypted secrets, most specific first. */
function secretDirs() {
  const dirs = [];
  if (process.env.LARKSUITE_CLI_DATA_DIR) dirs.push(join(process.env.LARKSUITE_CLI_DATA_DIR, "lark-cli"));
  dirs.push(join(homedir(), ".local", "share", "lark-cli"));
  dirs.push(join(homedir(), "Library", "Application Support", "lark-cli"));
  return dirs;
}

function masterKey(dir) {
  for (const name of ["master.key", "master.key.file"]) {
    const path = join(dir, name);
    if (!existsSync(path)) continue;
    const key = readFileSync(path);
    if (key.length === 32) return key;
  }
  if (process.platform !== "darwin") return null;
  try {
    // macOS: lark-cli keeps the master key in the login keychain, base64-encoded.
    let text = execFileSync("/usr/bin/security", ["find-generic-password", "-s", "lark-cli", "-a", "master.key", "-w"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    }).trim();
    const prefix = "go-keyring-base64:";
    if (text.startsWith(prefix)) text = Buffer.from(text.slice(prefix.length), "base64").toString("utf8");
    const key = Buffer.from(text, "base64");
    return key.length === 32 ? key : null;
  } catch {
    return null;
  }
}

/** lark-cli's format: 12-byte nonce, ciphertext, 16-byte tag; AES-256-GCM. */
function decrypt(data, key) {
  if (data.length < 28) return null;
  try {
    const decipher = createDecipheriv("aes-256-gcm", key, data.subarray(0, 12));
    decipher.setAuthTag(data.subarray(data.length - 16));
    return Buffer.concat([decipher.update(data.subarray(12, data.length - 16)), decipher.final()]).toString("utf8");
  } catch {
    return null;
  }
}

function storedSecret(account) {
  const file = `${account.replace(/[^a-zA-Z0-9._-]/g, "_")}.enc`;
  for (const dir of secretDirs()) {
    const path = join(dir, file);
    if (!existsSync(path)) continue;
    const key = masterKey(dir);
    const secret = key && decrypt(readFileSync(path), key);
    if (secret) return secret;
  }
  return null;
}

function credentials() {
  let config;
  try {
    config = JSON.parse(readFileSync(join(configDir(), "config.json"), "utf8"));
  } catch {
    fail("cannot read lark-cli's config.json");
  }
  const apps = Array.isArray(config.apps) ? config.apps : [];
  const wanted = (process.env.LARK_BOT_APP_ID || "").trim();
  const app =
    (wanted && apps.find((a) => a.appId === wanted)) ||
    apps.find((a) => config.currentApp && a.name === config.currentApp) ||
    (apps.length === 1 ? apps[0] : null);
  if (!app || !app.appId) fail("no app in lark-cli's config");
  const stored = app.appSecret;
  let secret = null;
  if (typeof stored === "string") secret = stored;
  else if (stored && stored.source === "keychain") secret = storedSecret(stored.id || `appsecret:${app.appId}`);
  if (!secret || secret.includes("*")) fail("cannot read the app secret from lark-cli's store");
  const brand = (process.env.LARK_BRAND || app.brand || "feishu").toLowerCase();
  return { appId: app.appId, appSecret: secret, brand: brand === "lark" ? "lark" : "feishu" };
}

function mentionOpenId(id) {
  if (typeof id === "string") return id;
  return id && typeof id.open_id === "string" ? id.open_id : "";
}

/** The flat object `lark-cli event consume im.message.receive_v1` prints. */
export function flatten(data) {
  const message = data.message || {};
  const sender = data.sender || {};
  const raw = Array.isArray(message.mentions) ? message.mentions : [];
  const mentions = raw
    .map((m) => ({ key: m.key || "", id: mentionOpenId(m.id), name: m.name || "" }))
    .filter((m) => m.key || m.id || m.name);
  const out = {
    type: EVENT,
    event_id: data.event_id,
    timestamp: data.create_time || message.create_time,
    id: message.message_id,
    message_id: message.message_id,
    create_time: message.create_time,
    chat_id: message.chat_id,
    chat_type: message.chat_type,
    message_type: message.message_type,
    sender_id: (sender.sender_id || {}).open_id,
    sender_type: sender.sender_type,
    root_id: message.root_id || undefined,
    thread_id: message.thread_id || undefined,
    reply_to: message.parent_id || undefined,
  };
  if (message.update_time && message.update_time !== message.create_time) out.update_time = message.update_time;
  let text = null;
  if (message.message_type === "text") {
    try {
      text = JSON.parse(message.content).text;
    } catch {
      text = null;
    }
  }
  if (typeof text === "string" && text) {
    // Longest key first, so @_user_1 does not eat the start of @_user_10.
    for (const m of [...mentions].sort((a, b) => b.key.length - a.key.length)) {
      if (m.key && m.name) text = text.split(m.key).join(`@${m.name}`);
    }
    out.content = text;
  } else {
    // Other types are rendered by lark-cli; relay.py fetches the text with `im +messages-mget`.
    out.content = "";
    out.content_pending = true;
  }
  if (mentions.length) out.mentions = mentions;
  return out;
}

async function main() {
  let sdk;
  try {
    sdk = await import("@larksuiteoapi/node-sdk");
  } catch {
    fail("@larksuiteoapi/node-sdk is not installed next to listen.mjs");
  }
  const { appId, appSecret, brand } = credentials();
  if (process.argv.includes("--check")) {
    note(`[listen] ok app_id=${appId} brand=${brand}`);
    process.exit(0);
  }
  const { Domain, EventDispatcher, LoggerLevel, WSClient } = sdk;
  const dispatcher = new EventDispatcher({ loggerLevel: LoggerLevel.error }).register({
    [EVENT]: async (data) => {
      const event = flatten(data || {});
      if (event.message_id) process.stdout.write(`${JSON.stringify(event)}\n`);
    },
  });
  const client = new WSClient({
    appId,
    appSecret,
    domain: brand === "lark" ? Domain.Lark : Domain.Feishu,
    loggerLevel: LoggerLevel.error,
    autoReconnect: true,
    source: "grokbot-lark",
    handshakeTimeoutMs: HANDSHAKE_TIMEOUT_MS,
    wsConfig: { pingTimeout: PING_TIMEOUT_SEC },
    onReady: () => note(`[event] ready event_key=${EVENT}`),
    onReconnecting: () => note("[listen] reconnecting"),
    onReconnected: () => note("[listen] reconnected"),
    onError: (error) => note(`[listen] error: ${String((error && error.message) || error).slice(0, 300)}`),
  });
  const stop = (reason) => {
    note(`[event] exited (reason: ${reason})`);
    try {
      client.close({ force: true });
    } catch {
      // closing an unopened client
    }
    process.exit(0);
  };
  process.on("SIGTERM", () => stop("signal"));
  process.on("SIGINT", () => stop("signal"));
  process.stdin.on("end", () => stop("stdin closed"));
  process.stdin.on("error", () => stop("stdin closed"));
  process.stdin.resume();
  await client.start({ eventDispatcher: dispatcher });
}

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop())) {
  main().catch((error) => {
    note(`[listen] failed: ${String((error && error.message) || error).slice(0, 300)}`);
    process.exit(1);
  });
}
