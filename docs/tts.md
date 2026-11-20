# Server-side speech

By default the relay writes a `.say` object (UTF-8 text) and the **phone's Android text-to-speech engine** speaks it. That needs no extra service, costs nothing, and uses whatever voice the phone has.

If you want a better voice, point the relay at any server that implements the OpenAI speech endpoint: `POST <url>/audio/speech` with `{"model", "input", "voice", "response_format"}`, returning audio bytes. Local servers (Kokoro, Piper wrappers and similar) and hosted ones both work.

```sh
EARSHOT_RELAY_TTS_URL=https://tts.example.com/v1
EARSHOT_RELAY_TTS_MODEL=your-model
EARSHOT_RELAY_TTS_VOICE=your-voice        # required: the default voice (the OpenAI API requires one)
EARSHOT_RELAY_TTS_API_KEY=...             # optional bearer token for the TTS server
EARSHOT_RELAY_TTS_FORMAT=mp3              # mp3 or wav
EARSHOT_RELAY_DEFAULT_SPEECH=device       # or server, to make server speech the default
```

Ask for it per message:

```sh
curl -fsS -X POST http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer $EARSHOT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Deploy finished", "speech": "server", "voice": "your-voice"}'
```

`voice` is only valid with server speech (using it with device speech is a 422), and the voice name is checked against a conservative character set before it is sent anywhere.

## ElevenLabs

```sh
EARSHOT_RELAY_TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=...                       # environment or ELEVENLABS_API_KEY_FILE only; never a file in a repo
EARSHOT_RELAY_TTS_VOICE=<voice id>           # required: an ElevenLabs voice id (a request's `voice` can override it)
EARSHOT_RELAY_TTS_MODEL=eleven_multilingual_v2   # optional; any model id your account can use (eleven_flash_v2_5, ...)
EARSHOT_RELAY_TTS_FORMAT=mp3                 # mp3 (mp3_44100_128) or opus (opus_48000_64)
```

The relay calls `POST https://api.elevenlabs.io/v1/text-to-speech/<voice id>?output_format=<format>` with
`{"text", "model_id"}` and the key in the `xi-api-key` header, then runs the same lead-in step below and uploads
Ogg/Opus. The key is read from the environment only, is never logged, and never appears in an error or in
`GET /v1/status` (which shows only `speech.provider`).

**What was tested:** the request shape, auth header, formats, error handling and key hygiene, against a stub server
in the test suite. **What was not:** a real ElevenLabs account. No real key was used, so the live response, your
plan's limits and the `opus_48000_64` format are unverified; if `opus` fails for you, use `mp3`. Failures behave as
for any server TTS (below): `502 tts_failed`, nothing written, no fallback.

## Local voices (no cloud, no key)

Any local server that speaks the OpenAI `/audio/speech` shape works through the default provider. These are the
settings each project documents for that endpoint. **None of them was run while writing this**: they are untested
here, so confirm with `earshot-relay say --speech server "test"` and a receipt before relying on them. (The adapter
itself is tested against a stub of that endpoint.)

| Server | `EARSHOT_RELAY_TTS_URL` | `..._MODEL` | `..._VOICE` | Status |
| --- | --- | --- | --- | --- |
| [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) | `http://kokoro:8880/v1` | `kokoro` | `af_bella` (any Kokoro voice name) | untested |
| [openedai-speech](https://github.com/matatonic/openedai-speech) (Piper or XTTS) | `http://openedai-speech:8000/v1` | `tts-1` | `alloy` (mapped to a Piper voice in its `voice_to_speaker.yaml`) | untested |
| [LocalAI](https://localai.io/features/text-to-audio/) | `http://localai:8080/v1` | the name of an installed TTS model, for example a Piper voice | that model's voice name, or the model name again | untested |

Use `EARSHOT_RELAY_TTS_FORMAT=mp3` first (`wav` also works if the server returns it). Put the relay and the TTS server
on one Docker network and use the container name as the host, as above. A server with no API key needs none here.

## The 700 ms lead-in

Phones often clip the first fraction of a second of audio while the audio route wakes up, which eats the start of a sentence. With `EARSHOT_RELAY_TTS_LEADIN_MS` above 0 (default 700) the relay runs ffmpeg once per message:

```
ffmpeg -i in -af adelay=700:all=1 -c:a libopus -b:a 48k out.opus
```

and uploads the result as `<channel>/<id>.opus` with `Content-Type: audio/ogg`. The container image includes ffmpeg. Outside Docker you need ffmpeg on the PATH, or set the lead-in to `0`, in which case the TTS output is uploaded unchanged as `.mp3` or `.wav`. The relay refuses to start if the lead-in is on and ffmpeg is missing.

## Failures never fall back

If the TTS server errors, returns a tiny or empty body, or times out, the request fails: `502 tts_failed` or `504 tts_timeout`. Nothing is written to the bucket. The relay does not quietly send the same text as a phone-voiced `.say`, because then you could not tell which voice you got, and text you meant to be synthesized would go out in a different form. If you want a fallback, do it in the caller: retry with `"speech": "device"`.

The TTS API key is never logged and never appears in error responses.
