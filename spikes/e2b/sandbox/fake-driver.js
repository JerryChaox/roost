// Stand-in for roost-driver: a dependency-free HTTP server used to probe
// E2B's port endpoint (plain responses, SSE, idle waits) and process
// lifecycle (pause/resume keeps memory, reboot does not).
const http = require('http')
const os = require('os')
const fs = require('fs')

const port = Number(process.env.PORT || 8080)
const bootId = fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim()
const startedAt = Date.now()
let ticks = 0 // in-memory state: survives a memory snapshot, not a reboot
setInterval(() => ticks++, 1000)

// The driver reads its grant from the environment and clears it.
const grant = process.env.ROOST_GRANT || null
delete process.env.ROOST_GRANT

http
  .createServer((req, res) => {
    const u = new URL(req.url, 'http://x')
    if (u.pathname === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(
        JSON.stringify({
          ok: true,
          port,
          pid: process.pid,
          uid: process.getuid(),
          user: os.userInfo().username,
          bootId,
          startedAt,
          ticks,
          uptimeSec: os.uptime(),
          grantSeen: grant ? `len=${grant.length}` : null,
          envStillHasGrant: 'ROOST_GRANT' in process.env,
          headers: req.headers,
        }) + '\n'
      )
      return
    }
    if (u.pathname === '/sse') {
      // n events, one every `every` ms; first one immediately.
      const n = Number(u.searchParams.get('n') || 5)
      const every = Number(u.searchParams.get('every') || 1000)
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
      let i = 0
      const send = () => {
        res.write(`data: {"i":${i},"sentAt":${Date.now()}}\n\n`)
        if (++i >= n) {
          clearInterval(t)
          res.end()
        }
      }
      const t = setInterval(send, every)
      send()
      req.on('close', () => clearInterval(t))
      return
    }
    if (u.pathname === '/idle-sse') {
      // headers + one event, then silence for `s` seconds, then a final event.
      const s = Number(u.searchParams.get('s') || 60)
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
      res.write(`data: {"phase":"start","sentAt":${Date.now()}}\n\n`)
      const t = setTimeout(() => {
        res.end(`data: {"phase":"after-idle","idleSec":${s},"sentAt":${Date.now()}}\n\n`)
      }, s * 1000)
      req.on('close', () => clearTimeout(t))
      return
    }
    if (u.pathname === '/sleep') {
      // no bytes at all for `s` seconds, then a response.
      const s = Number(u.searchParams.get('s') || 1)
      setTimeout(() => {
        res.writeHead(200)
        res.end(`slept ${s}\n`)
      }, s * 1000)
      return
    }
    res.writeHead(404)
    res.end('not found\n')
  })
  .listen(port, '0.0.0.0', () => console.log(`fake-driver listening on ${port} pid=${process.pid}`))
