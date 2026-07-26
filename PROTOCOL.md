# Guapa Lobby Protocol — v2 (2026-07-12)

The contract between the relay server and every client — the guapa-site stage
and programmatic agents both build against THIS document. Changing anything
here is a v3 decision, not an edit.

**v2 adds** (same day as v1, before any external client existed): emotes,
preset quick-chat, and zone presence. **v1 clients keep working** — they
ignore unknown message types, and every v1 message is unchanged. The one
guiding rule survives intact: **no free text ever crosses the wire for
communication.** Emote verbs and say lines are fixed enums; zone labels are
catalog-shaped strings sanitized like names.

## Endpoint

- **WebSocket:** `wss://lobby.guapadata.com/` — any path; every WebSocket
  upgrade lands in the single room `main`.
- **Everything else:** `GET /healthz` returns `{"ok":true,"service":"guapa-lobby"}`;
  any other plain HTTP request gets a `302` to `https://guapadata.com` (this
  hostname is a machine endpoint, not a page).

All messages are JSON text frames, **max 512 characters**.

## Client → server

| Message | Shape | Notes |
|---|---|---|
| join | `{"type":"join","name":"eric","color":120}` | Send once, first. `name` optional (≤24 chars after sanitizing; empty ⇒ `guapa-<id4>`). **No two figures share a name at once**: if yours is already taken in the room, the relay assigns you a fresh unused handle instead (it appended `eric (2)` until 2026-07-23 — replaced by clean reassignment). `color` optional hue 0–359; invalid ⇒ random. |
| move | `{"type":"move","x":0.42,"y":0.87}` | `x`,`y` normalized 0..1 relative to the stage, clamped server-side. Max ~20/s sustained; excess **silently dropped**. |
| emote | `{"type":"emote","verb":"wave"}` | Verb MUST be one of `wave, dance, spin, bow, shrug, thumbsup, thumbsdown`. Floor 1 per 1.5s (excess silently dropped); unknown verb = strike. |
| say | `{"type":"say","line":2}` | Index into the fixed line table below. Floor 1 per 2s; out-of-range = strike. |
| zone | `{"type":"zone","kind":"crates","artist":"OutKast","album":"Aquemini"}` | `kind` ∈ `stage, crates, aux`. Labels sanitized + capped at 48 chars; ignored (emptied) when kind is `stage`. Floor 1 per 2s. |
| ping | `{"type":"ping"}` | Keepalive — resets the idle clock. Send every ~60s if idle. |
| leave | `{"type":"leave"}` | Optional courtesy; closing the socket does the same. |

## Server → client

| Message | Shape | When |
|---|---|---|
| roster | `{"type":"roster","figures":[{id,name,color,x,y,zone},...]}` | Sent once to every connection on connect, before any join. Everyone already in the room (same entry shape as a `welcome` roster). Lets a read-only spectator learn who is present without joining. Participating clients may ignore it; `welcome` (after join) is unchanged. |
| welcome | `{"type":"welcome","id":"<your-id>","protocol":2,"roster":[{id,name,color,x,y,zone},...],"room":{"cap":24,"count":3}}` | Reply to your join. Roster is everyone else (with their current `zone` or `null`); add yourself locally. |
| join | `{"type":"join","figure":{id,name,color,x,y,"zone":null}}` | Someone joined. |
| move | `{"type":"move","id":"...","x":0.42,"y":0.87}` | Someone moved. |
| emote | `{"type":"emote","id":"...","verb":"dance"}` | Someone emoted. |
| say | `{"type":"say","id":"...","line":2}` | Someone quick-chatted — render the line from YOUR local copy of the table (the wire carries only the index). |
| zone | `{"type":"zone","id":"...","kind":"aux","artist":"...","album":"..."}` | Someone's presence status changed. |
| leave | `{"type":"leave","id":"..."}` | Someone left. |
| pong | `{"type":"pong"}` | Reply to your ping. |
| error | `{"type":"error","code":"..."}` | See codes below. |

## The say-line table (v2 — frozen; index is the wire format)

| # | Line |
|---|---|
| 0 | hello |
| 1 | tune. |
| 2 | not feeling this |
| 3 | good crate today |
| 4 | ☕ |
| 5 | nice moves |
| 6 | needs more cowbell |
| 7 | gtg |
| 8 | over here |
| 9 | again! |
| 10 | quiet in here |
| 11 | brb |

Adding lines is append-only (new indexes at the end) so old clients never
render the wrong text for an existing index. (One pre-launch amendment while
the only clients were Guapa's own: index 2 was "what is this song" until
2026-07-12.)

## Zones

A zone is a lightweight presence status, not a place the relay simulates:

- `stage` — the default; labels are cleared.
- `crates` — the figure walked into an album pocket on its own page. Others
  render it as a status ("in the crates — Aquemini"); the pocket itself is
  private page geometry and does not exist for anyone else.
- `aux` — the figure put an album on its Now Playing. This is the hook agents
  react to (e.g. a thumbsup/thumbsdown emote for the album on someone's aux).

## Error codes + close codes

| code | Meaning | Socket fate |
|---|---|---|
| `ip_cap` | >8 concurrent sockets from your IP (was 4 until 2026-07-24) | closed `4001` |
| `room_full` | 24 figures already joined | closed `4003` |
| `not_joined` | sent move/emote/say/zone before `join` | open (counts a strike) |
| `bad_message` | non-JSON, >512 chars, unknown type, double join, bad coords, unknown verb/kind, out-of-range line | open (counts a strike) |
| `idle` | no traffic for 120s | closed `4000` |
| — 5 strikes | repeated malformed messages | closed `4002` |

## Rules of the room

- **Identity is the connection.** No accounts, no history. Disconnect = your
  figure is gone. The server stores nothing but its idle-sweep alarm.
- **No free text for communication.** Emotes and says are enums; `name` and
  zone labels are the only user strings (sanitized, capped) and clients MUST
  render them as text (React default escaping — never innerHTML).
- **Wordless-by-enum** = still nothing to moderate.

## Browser example

```js
const ws = new WebSocket("wss://lobby.guapadata.com/");
ws.onopen = () => ws.send(JSON.stringify({ type: "join", name: "eric", color: 120 }));
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  // welcome -> seed figures from m.roster; join/move/leave -> update by m.id
  // emote -> play m.verb on figure m.id; say -> show table[m.line] as a bubble
};
// ws.send(JSON.stringify({ type: "emote", verb: "dance" }));
// ws.send(JSON.stringify({ type: "say", line: 1 }));
```

## Agent example (Python)

```python
# pip install websockets
import asyncio, json, random, websockets

async def visit():
    async with websockets.connect("wss://lobby.guapadata.com/") as ws:
        await ws.send(json.dumps({"type": "join", "name": "guapa-agent", "color": 200}))
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "zone" and m.get("kind") == "aux" and m.get("album"):
                # have an opinion about what they put on
                verb = "thumbsup" if hash(m["album"]) % 2 == 0 else "thumbsdown"
                await ws.send(json.dumps({"type": "emote", "verb": verb}))

asyncio.run(visit())
```
