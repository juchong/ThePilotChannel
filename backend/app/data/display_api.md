# The Pilot Channel display API

The Pilot Channel is a TV display for an aircraft hangar. It cycles through views: local
traffic maps, regional weather, a satellite loop, live cameras and NOTAMs. A program on the
same network can read which view is on screen, move the display to the next or previous
view or straight to a chosen one, and black out the picture.

This document is the whole contract for those calls. The display serves other routes for
the TV itself and for its admin page; they are not part of this API.

## Connecting

- Base URL: `http://<pi-ip>:8000`. Every path below is relative to it.
- Plain HTTP on the local network; the display does not serve HTTPS.
- Request and answer bodies are JSON. Send `Content-Type: application/json` with a body: a
  body sent without it, or as another type, answers 422.
- Times are Unix timestamps in seconds (UTC) and may have a fraction. Durations are in
  seconds.
- None of these calls is rate limited.

## Authentication

- `GET` calls need no key.
- Every `POST` call needs an API key, sent as the header `Authorization: Bearer <key>`.
  A key starts with `tpc_`. The display's owner creates it on the admin page (Access,
  API keys), where it is shown once. This document never contains a key: keep it as a
  secret (in Home Assistant, in `secrets.yaml`), never in code or in logs.
- A key that may control the display is enough for every call here. A key that may also
  change settings works too.
- The admin password is not accepted, neither as HTTP Basic nor as a bearer value.

## Errors

A refused call answers with a status code and a JSON body whose `detail` says why.

| Status | When | `detail` |
|---|---|---|
| 401 | A `POST` without an `Authorization` header | `"sign in, or send an API key"` |
| 401 | A key that is not recognized: mistyped, revoked, or not sent as `Bearer` | `"API key not recognized"` |
| 404 | `POST /api/display/view` names a view the display does not have | `"unknown view"` |
| 422 | The body is not JSON, or a field breaks its rule | a list of `{"loc", "msg", "type"}` objects; `loc` names the field, for example `["body", "seconds"]` |
| 503 | The display cannot read its key store, so it cannot check a key | a sentence naming the repair |
| 503 | `GET /api/stream` when too many event streams are open | `"too many event subscribers"` |

A 401 answer carries the header `WWW-Authenticate: Bearer`. A 401 or 422 call sent again
unchanged gets the same answer; a 404 too, until the owner adds that view.

## Reading what is on screen

### GET /api/display

No key. The blackout state, the last request to move the display, and the view the TV
reports it is showing.

```json
{
  "active": false,
  "until": null,
  "reason": null,
  "remaining_s": null,
  "nav": {"seq": 12, "action": "view", "view": "satellite", "ts": 1794650847.7, "age_s": 33.9},
  "showing": {"view": "satellite", "index": 6, "since": 1794650848.1, "label": "GOES-West PNW GeoColor", "count": 10, "age_s": 33.5}
}
```

| Field | Meaning |
|---|---|
| `active` | `true` while the picture is blacked out |
| `until` | when a timed blackout ends; `null` when it lasts until restored, or when there is none |
| `reason` | the reason sent with the blackout, or `null` |
| `remaining_s` | seconds left of a timed blackout, else `null` |
| `nav.seq` | the number of the last request to move the display: one more with each request, 0 before the first |
| `nav.action` | `"next"`, `"previous"` or `"view"`; `null` before the first request |
| `nav.view` | the view id a `"view"` request named, else `null` |
| `nav.ts`, `nav.age_s` | when that request was made, and how many seconds ago; `null` before the first request |
| `showing.view` | the id of the view the TV reports it is showing; `null` until the TV has reported one |
| `showing.label` | that view's title on the TV |
| `showing.index` | its position in the cycle, from 0, as in `GET /api/views`; `null` when unknown |
| `showing.count` | the number of entries in the cycle |
| `showing.since`, `showing.age_s` | when the TV switched to that view, and how many seconds ago |

When the owner saves new settings, the TV reloads and `showing` is `null` until it reports
again. When the display's software restarts, a blackout ends, `nav.seq` starts again from
0 and `showing` is `null` until the TV reports.

### GET /api/views

No key. The views in the order the TV cycles through them.

```json
{
  "views": [
    {"id": "local:KSEA", "type": "local", "label": "KSEA - Local 5mi", "dwell_s": 20},
    {"id": "region:Puget Sound", "type": "regional", "label": "Region: Puget Sound - 30mi", "dwell_s": 15},
    {"id": "satellite", "type": "satellite", "label": "GOES-West PNW GeoColor", "dwell_s": 25}
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | what `POST /api/display/view` takes |
| `type` | `local`, `regional`, `satellite`, `cameras` or `notams` |
| `label` | the view's title on the TV |
| `dwell_s` | seconds the view stays on screen |

Each view also carries fields the TV uses to draw it; a client can ignore them.

Ids have these forms: `local:<airport>` (`local:KSEA`), `region:<region name>`
(`region:Puget Sound`, spaces included), `satellite`, `cameras`, and `notams:<location>`
(`notams:KS50`). The same view can appear more than once in the cycle, for example a
region view between airport views; `showing.index` and `showing.count` count every
appearance. The list changes when the owner changes the settings: read it again after a
`config_changed` event or a 404 from `POST /api/display/view`.

## Moving the display

### POST /api/display/next

Key needed, no body. Moves the display on to the next view in the cycle, wrapping from the
last to the first.

### POST /api/display/previous

Key needed, no body. Moves the display back to the previous view, wrapping from the first
to the last.

### POST /api/display/view

Key needed. Body: `{"view": "<id>"}`, a view id from `GET /api/views` of at most 100
characters. Moves the display straight to that view. When the id appears more than once in
the cycle, the display goes to its first appearance counting from the view on screen. The
view already on screen stays, and its time on screen starts again. An id the display does
not have answers 404 `"unknown view"`.

### How a move works

- The answer (200) is the display state, as `GET /api/display` gives it, right after the
  request was recorded: `nav` shows the request, while `showing` still names the view that
  was on screen before it.
- The request reaches the TV over its event stream and the TV applies it once. A request
  that reaches the TV more than 15 seconds after it was made is dropped (the TV was
  reconnecting), and while the TV is off or not connected nothing changes on screen. The
  call answers 200 in every case: to know the TV switched, read `GET /api/display` until
  `showing.view` names the view.
- The view moved to stays on screen for its own `dwell_s`, then the cycle carries on from
  there.
- Moving the display does not end a blackout: the view changes behind the black picture.

## Blacking out the picture

### POST /api/display/blackout

Key needed. Blacks out the TV picture. The body is optional; no body, an empty body and
`{}` all black out until `POST /api/display/restore`.

| Field | Rule | Meaning |
|---|---|---|
| `seconds` | a number above 0, at most 86400 | how long the picture stays black; leave out, or `null`, to keep it black until restored |
| `reason` | a string of at most 120 characters | reported by `GET /api/display`, shown on the admin page and written to the display's log |

The answer (200) is the display state with `active: true`. A blackout replaces one already
in progress, end time included: a 30-second blackout sent during one that lasts until
restored brings the picture back after 30 seconds. A timed blackout ends on its own. The
display keeps running behind the black picture, so it comes back at once.

### POST /api/display/restore

Key needed, no body. Ends a blackout. The answer (200) is the display state with
`active: false`; it is the same when nothing was blacked out.

## Following changes

### GET /api/stream

No key. A stream of server-sent events (`text/event-stream`) that stays open. Each event is
one `data:` line holding a JSON object whose `event` field names it; the stream sends no
SSE `event:` or `id:` fields, so read the default message event. A line starting with `:`
is a keep-alive comment, sent after 20 seconds without an event.

| `event` | Sent | Other fields |
|---|---|---|
| `connected` | first, on every connection | `boot_id` (a new value after the display's software restarted), `version` (of the settings), `display` (as `GET /api/display`) |
| `display` | after every blackout or restore call, when a timed blackout ends, and when a request to move the display is recorded | the fields of `GET /api/display` |
| `config_changed` | when the owner saved new settings: the views may have changed | `version` |
| `weather_updated`, `notams_updated` | when the TV's own data was refreshed | none a client needs |

No event is sent when the TV reports that it switched views: to see `showing` change,
read `GET /api/display`. At most 32 event streams can be open at once, the TV's own
included; beyond that the call answers 503. After a dropped connection, connect again: the
`connected` event carries the current state.

## Views on this display

<!-- views -->
The views depend on the display's settings. For example:

| id | type | label | seconds on screen | entries in the cycle |
|---|---|---|---|---|
| `local:KSEA` | local | KSEA - Local 5mi | 20 | 1 |
| `region:Puget Sound` | regional | Region: Puget Sound - 30mi | 15 | 2 |
| `satellite` | satellite | GOES-West PNW GeoColor | 25 | 1 |
| `cameras` | cameras | Airport Cameras | 30 | 1 |
| `notams:KS50` | notams | KS50 - NOTAMs | 30 | 1 |
<!-- /views -->

## Examples

```bash
BASE=http://<pi-ip>:8000
KEY=tpc_your-key

# what is on screen, and the views
curl -s "$BASE/api/display"
curl -s "$BASE/api/views"

# next, previous, and straight to a view
curl -s -X POST -H "Authorization: Bearer $KEY" "$BASE/api/display/next"
curl -s -X POST -H "Authorization: Bearer $KEY" "$BASE/api/display/previous"
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"view": "satellite"}' "$BASE/api/display/view"

# black out for 30 seconds; black out until restored; restore
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"seconds": 30, "reason": "doorbell"}' "$BASE/api/display/blackout"
curl -s -X POST -H "Authorization: Bearer $KEY" "$BASE/api/display/blackout"
curl -s -X POST -H "Authorization: Bearer $KEY" "$BASE/api/display/restore"

# follow changes as they happen
curl -sN "$BASE/api/stream"
```

## Not part of this API

- `POST /api/display/showing` is how the TV reports the view it switched to. A call from
  anything else makes `GET /api/display` name a view that is not on screen.
- Settings, API keys and the account are managed on the display's admin page.
