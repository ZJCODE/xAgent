# Realtime Walkthrough

This walkthrough exercises the realtime gateway end to end:

1. realtime-tier local tools
2. foreground text turns with partial updates
3. background Responses jobs
4. interrupt handling
5. audio chunk commit flow

## 1) Install the extra demo dependency

```bash
pip install websockets
```

## 2) Start the showcase server

```bash
export OPENAI_API_KEY=your_key_here
python examples/demo/realtime_showcase_server.py --port 8011
```

The server exposes:

- HTTP API: `http://localhost:8011`
- Realtime websocket: `ws://localhost:8011/realtime/ws`

## 3) Start the interactive realtime client

In another terminal:

```bash
python examples/demo/realtime_client.py --ws-url ws://127.0.0.1:8011/realtime/ws
```

After `session.state`, run the following commands.

## 4) Test realtime-tier local tools

```text
/tool pause_music pause the studio speakers
/tool set_status_light set the light to amber
```

Expected behavior:

- immediate `ack`
- `turn.started`
- `turn.completed`
- no background job

## 5) Test a foreground text turn

```text
Summarize why separating realtime orchestration from deep reasoning reduces latency.
```

Expected behavior:

- `ack`
- `turn.started`
- one or more `partial_text`
- `turn.completed`

## 6) Test a background Responses job

```text
search the latest AI agent framework news
```

Expected behavior:

- `ack`
- `turn.started`
- `job.started`
- `job.progress`
- `job.completed`

The server routes this turn into the background job path because the text contains a complex-task hint such as `search`.

## 7) Test interrupt handling

Start a long-running turn or background job, then send:

```text
/interrupt
```

Expected behavior:

- the active session is marked interrupted
- any active background job is cancelled

## 8) Test audio chunk commit

Prepare a provider-compatible audio payload, then run:

```bash
python examples/demo/realtime_audio_sender.py --ws-url ws://127.0.0.1:8011/realtime/ws --file ./sample_audio.raw
```

Expected behavior with a connected realtime provider:

- `session.state`
- one or more `ack` events for `input.audio.chunk`
- `ack` for `turn.commit`
- `turn.started`
- provider-driven realtime events or `turn.completed`

If the realtime provider is not available, the gateway returns an explicit error in `turn.completed`.

## Notes

- The interactive client is text-first. It exercises the same websocket event protocol as audio.
- The audio sender only base64-encodes file chunks and sends them. It does not transcode audio.
- Use `GET /jobs/{job_id}` and `POST /jobs/{job_id}/cancel` to inspect or cancel background jobs outside the websocket flow.
