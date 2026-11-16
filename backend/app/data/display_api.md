# The Pilot Channel display API

The Pilot Channel is a TV display for an aircraft hangar. It cycles through views: local
traffic maps, regional weather, a satellite loop, live cameras and NOTAMs. A program on the
same network can read which view is on screen, move the display to the next or previous
view or straight to a chosen one, black out the picture, and read the weather at the
airports the display watches.

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

- `GET /api/conditions` and every `POST` call need an API key, sent as the header
  `Authorization: Bearer <key>`; the other `GET` calls need no key.
- A key starts with `tpc_`. The display's owner creates it on the admin page (Access, API
  keys), where it is shown once. This document never contains a key: keep it as a secret
  (in Home Assistant, in `secrets.yaml`), never in code or in logs.
- A key that may control the display is enough for every call here. A key that may also
  change settings works too.
- The admin password is not accepted, neither as HTTP Basic nor as a bearer value.

## Errors

A refused call answers with a status code and a JSON body whose `detail` says why.

| Status | When | `detail` |
|---|---|---|
| 401 | A call that needs a key, sent without an `Authorization` header | `"sign in, or send an API key"` |
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

## Reading the weather

### GET /api/conditions

Key needed. The weather at every airport the display watches, from the latest METAR it
holds for each: flight category, ceiling, visibility, wind, temperature, dew point,
relative humidity and altimeter setting.

```json
{
  "monitored": ["KBFI", "KS50"],
  "airports": {
    "KBFI": {
      "name": "Boeing Field",
      "reported": true,
      "category": "MVFR",
      "observed": 1794808380,
      "age_s": 312,
      "stale": false,
      "ceiling_ft": 2500,
      "ceiling_known": true,
      "visibility_sm": 10.0,
      "wind_dir_deg": 160,
      "wind_variable": false,
      "wind_speed_kt": 7,
      "wind_gust_kt": 18,
      "temperature_c": 8.3,
      "dewpoint_c": 6.1,
      "humidity_pct": 86,
      "altimeter_inhg": 30.12,
      "raw": "KBFI 160553Z 16007G18KT 10SM FEW012 BKN025 08/06 A3012 RMK AO2 T00830061"
    },
    "KS50": {
      "name": "Auburn",
      "reported": false,
      "category": "UNKNOWN",
      "observed": null,
      "age_s": null,
      "stale": null,
      "ceiling_ft": null,
      "ceiling_known": false,
      "visibility_sm": null,
      "wind_dir_deg": null,
      "wind_variable": false,
      "wind_speed_kt": null,
      "wind_gust_kt": null,
      "temperature_c": null,
      "dewpoint_c": null,
      "humidity_pct": null,
      "altimeter_inhg": null,
      "raw": null
    }
  }
}
```

| Field | Meaning |
|---|---|
| `monitored` | the identifiers of the airports the display watches, in the order of its settings |
| `airports` | each of those airports' weather, by identifier, with the fields below |
| `name` | the airport's name in the display's settings, else the station's own |
| `reported` | `false` when the display holds no report for the airport: `category` is then `UNKNOWN`, `ceiling_known` and `wind_variable` are `false`, and every other value is `null` |
| `category` | `VFR`, `MVFR`, `IFR` or `LIFR`; `UNKNOWN` when the report does not settle one |
| `observed`, `age_s` | when the report was observed, and how many seconds ago |
| `stale` | `true` when the report is older than the display's limit (75 minutes unless the owner changed it) |
| `ceiling_ft` | the lowest broken or overcast layer, or the vertical visibility, in feet above the airport; `null` when there is no ceiling or its height is not known |
| `ceiling_known` | `true` when the report settles the ceiling (`ceiling_ft` then gives it, or is `null` for no ceiling); `false` when the report gives no sky condition or a ceiling without its height |
| `visibility_sm` | prevailing visibility in statute miles; a U.S. report gives 10 for 10 or more |
| `wind_dir_deg` | the direction the wind blows from, in degrees true; `null` when it is calm or variable |
| `wind_variable` | `true` when the report gives the direction as variable |
| `wind_speed_kt`, `wind_gust_kt` | wind speed and gusts in knots: 0 is calm, and `wind_gust_kt` is `null` without gusts |
| `temperature_c`, `dewpoint_c` | degrees Celsius, in tenths when the report gives them |
| `humidity_pct` | relative humidity in whole percent, worked out from the temperature and the dew point as the National Weather Service does for its observations |
| `altimeter_inhg` | the altimeter setting in inches of mercury |
| `raw` | the report as issued |

Every field is always there; a value the report does not give is `null`. The answer comes
from the reports the display already holds, which it refreshes every 5 minutes unless the
owner changed that, so asking more often than once a minute gains nothing. An airport the
owner turns off or removes leaves the answer.

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

# the weather at the airports the display watches
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/conditions"

# follow changes as they happen
curl -sN "$BASE/api/stream"
```

## Not part of this API

- `POST /api/display/showing` is how the TV reports the view it switched to. A call from
  anything else makes `GET /api/display` name a view that is not on screen.
- Settings, API keys and the account are managed on the display's admin page.
