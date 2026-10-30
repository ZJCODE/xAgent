/** Media durations, independent of packet size and the AudioContext sample rate. */
export const PLAYBACK_BUFFER_SECONDS = 0.2;
// Headroom above the server's 1 s outstanding-media window, including decoder bursts.
export const MAX_PLAYBACK_QUEUE_SECONDS = 1.2;
