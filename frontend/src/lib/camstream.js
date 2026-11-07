// Live camera tile: plays one go2rtc stream into a <video> element through Media
// Source Extensions. go2rtc's WebSocket protocol in "mse" mode: the client sends
// {type: "mse", value: "<comma-separated codecs>"}, the server answers
// {type: "mse", value: "<MIME type with codecs>"} and then streams binary
// fragmented-MP4 segments. Only video codecs are offered, so no audio is sent.
//
// A tile keeps a short live buffer and trims what has played, stays near the live
// edge (gently faster when behind, a jump when far behind), reconnects with backoff
// after any failure, and reports its state so the view can show "no signal".

export const VIDEO_CODECS = ["hvc1.1.6.L153.B0", "avc1.640033", "avc1.64002A", "avc1.640029"];
export const BUFFER_KEEP_S = 4; // played video kept behind the playhead before trimming
export const CATCH_UP_S = 1.0; // behind the live edge by more than this -> play slightly faster
export const JUMP_S = 3.0; // behind by more than this -> jump to the live edge
export const SIGNAL_TIMEOUT_MS = 8000; // no data for this long -> "waiting", reconnect
export const RECONNECT_MIN_MS = 1000;
export const RECONNECT_MAX_MS = 15000;
export const STABLE_MS = 30000; // this long with data resets the backoff

export function supportedCodecs(isTypeSupported, codecs = VIDEO_CODECS) {
  return codecs.filter((c) => isTypeSupported(`video/mp4; codecs="${c}"`));
}

export class CamStream {
  // video: the element to play into (muted, autoplay, playsinline).
  // url: the relay's WebSocket URL for this stream.
  // Dependencies can be injected for tests; the defaults are the browser's.
  constructor(video, url, opts = {}) {
    this.video = video;
    this.url = url;
    this.onState = opts.onState || (() => {});
    this.WebSocketImpl = opts.WebSocketImpl || WebSocket;
    this.MediaSourceImpl = opts.MediaSourceImpl || MediaSource;
    this.createObjectURL = opts.createObjectURL || ((ms) => URL.createObjectURL(ms));
    this.revokeObjectURL = opts.revokeObjectURL || ((u) => URL.revokeObjectURL(u));
    this.now = opts.now || (() => Date.now());
    this.signalTimeoutMs = opts.signalTimeoutMs ?? SIGNAL_TIMEOUT_MS;
    this.reconnectMinMs = opts.reconnectMinMs ?? RECONNECT_MIN_MS;
    this.reconnectMaxMs = opts.reconnectMaxMs ?? RECONNECT_MAX_MS;
    this.state = "stopped"; // stopped | connecting | live | waiting
    this.ws = null;
    this.ms = null;
    this.sb = null;
    this.queue = [];
    this.reconnectMs = this.reconnectMinMs;
    this.reconnectTimer = null;
    this.watchTimer = null;
    this.lastDataAt = 0;
    this.liveSince = 0;
    this.objectUrl = null;
    this.onVideoError = () => this.fail("video error");
  }

  start() {
    if (this.state !== "stopped" && this.state !== "waiting") return;
    this.setState("connecting");
    this.video.addEventListener("error", this.onVideoError);
    this.open();
    if (!this.watchTimer) this.watchTimer = setInterval(() => this.watch(), 1000);
  }

  stop() {
    this.setState("stopped");
    clearTimeout(this.reconnectTimer);
    clearInterval(this.watchTimer);
    this.reconnectTimer = this.watchTimer = null;
    this.video.removeEventListener("error", this.onVideoError);
    this.teardown();
    // Dropping the source and reloading releases the decoder at once.
    this.video.removeAttribute("src");
    if (typeof this.video.load === "function") this.video.load();
  }

  // ---- connection ---------------------------------------------------------
  open() {
    const ws = new this.WebSocketImpl(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => this.attachMediaSource();
    ws.onmessage = (e) => this.onMessage(e.data);
    ws.onclose = () => this.fail("connection closed");
    ws.onerror = () => {};
  }

  attachMediaSource() {
    const MS = this.MediaSourceImpl;
    const ms = new MS();
    this.ms = ms;
    ms.addEventListener(
      "sourceopen",
      () => {
        if (this.ms !== ms) return;
        this.send({ type: "mse", value: supportedCodecs(MS.isTypeSupported).join() });
      },
      { once: true }
    );
    this.objectUrl = this.createObjectURL(ms);
    this.video.src = this.objectUrl;
  }

  send(obj) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  onMessage(data) {
    if (typeof data === "string") {
      let msg;
      try {
        msg = JSON.parse(data);
      } catch (_) {
        return;
      }
      if (msg.type === "mse") this.openSourceBuffer(msg.value);
      else if (msg.type === "error") this.fail(String(msg.value || "stream error"));
      return;
    }
    this.lastDataAt = this.now();
    if (this.state !== "live") {
      this.liveSince = this.lastDataAt;
      this.setState("live");
    }
    this.queue.push(new Uint8Array(data));
    this.flush();
  }

  openSourceBuffer(mime) {
    if (!this.ms || this.ms.readyState !== "open") return;
    let sb;
    try {
      sb = this.ms.addSourceBuffer(mime);
    } catch (e) {
      this.fail(`unsupported stream: ${mime}`);
      return;
    }
    sb.mode = "segments";
    sb.addEventListener("updateend", () => this.onUpdateEnd(sb));
    this.sb = sb;
    this.flush();
  }

  // Appends what has arrived as one buffer, or waits for the current update.
  flush() {
    const sb = this.sb;
    if (!sb || sb.updating || !this.queue.length) return;
    let data;
    if (this.queue.length === 1) data = this.queue[0];
    else {
      const total = this.queue.reduce((n, c) => n + c.byteLength, 0);
      data = new Uint8Array(total);
      let off = 0;
      for (const c of this.queue) {
        data.set(c, off);
        off += c.byteLength;
      }
    }
    this.queue = [];
    try {
      sb.appendBuffer(data);
    } catch (e) {
      this.fail(`append failed: ${e.name || e}`);
    }
  }

  onUpdateEnd(sb) {
    if (sb !== this.sb || sb.updating) return;
    if (this.queue.length) {
      this.flush();
      return;
    }
    this.trimAndChase(sb);
    if (this.video.paused && typeof this.video.play === "function") {
      const p = this.video.play();
      if (p && p.catch) p.catch(() => {});
    }
  }

  // Keeps the buffer short and the playhead near the live edge.
  trimAndChase(sb) {
    const b = sb.buffered;
    if (!b || !b.length) return;
    const start = b.start(0);
    const end = b.end(b.length - 1);
    const v = this.video;
    if (v.currentTime < b.start(b.length - 1)) v.currentTime = b.start(b.length - 1);
    const behind = end - v.currentTime;
    if (behind > JUMP_S) v.currentTime = end - 0.5;
    v.playbackRate = behind > CATCH_UP_S ? 1.1 : 1;
    const keepFrom = v.currentTime - BUFFER_KEEP_S;
    if (keepFrom > start && !sb.updating) {
      try {
        sb.remove(start, keepFrom);
      } catch (_) {}
    }
  }

  // ---- failure, watchdog, reconnect ----------------------------------------
  watch() {
    if (this.state === "live" && this.now() - this.lastDataAt > this.signalTimeoutMs) {
      this.fail("no data");
      return;
    }
    if (this.state === "live" && this.now() - this.liveSince > STABLE_MS) this.reconnectMs = this.reconnectMinMs;
  }

  fail(reason) {
    if (this.state === "stopped") return;
    this.lastReason = reason;
    this.teardown();
    this.setState("waiting");
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.state === "waiting") this.start();
    }, this.reconnectMs);
    this.reconnectMs = Math.min(this.reconnectMs * 2, this.reconnectMaxMs);
  }

  teardown() {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try {
        ws.close();
      } catch (_) {}
    }
    this.sb = null;
    this.ms = null;
    this.queue = [];
    if (this.objectUrl) {
      this.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
  }

  setState(s) {
    if (s === this.state) return;
    this.state = s;
    this.onState(s, this);
  }
}
