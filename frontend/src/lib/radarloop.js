// Radar loop stepping. A frame is shown only once its tiles are loaded, with a
// bounded wait so a failing tile server cannot stall the loop.
export const RADAR_FRAME_MS = 550; // dwell per frame
export const RADAR_HOLD_MS = 1800; // pause on the newest frame each loop
export const RADAR_WAIT_MS = 100; // poll interval while a frame's tiles load
export const RADAR_WAIT_MAX_MS = 3000; // show the frame anyway after this

// -> { show, nextMs }: whether to show the frame now and when to run again.
export function radarStep({ ready, waitedMs, last }) {
  if (!ready && waitedMs < RADAR_WAIT_MAX_MS) return { show: false, nextMs: RADAR_WAIT_MS };
  return { show: true, nextMs: last ? RADAR_HOLD_MS : RADAR_FRAME_MS };
}
