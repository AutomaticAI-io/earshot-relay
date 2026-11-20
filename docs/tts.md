# Server-side speech

By default the relay writes a `.say` object (UTF-8 text) and the **phone's Android text-to-speech engine** speaks it. That needs no extra service, costs nothing, and uses whatever voice the phone has.

If you want a better voice, point the relay at any server that implements the OpenAI speech endpoint: `POST <url>/audio/speech` with `{"model", "input", "voice", "response_format"}`, returning audio bytes. Local servers (Kokoro, Piper wrappers and similar) and hosted ones both work.

```sh
EARSHOT_RELAY_TTS_URL=https://tts.example.com/v1
EARSHOT_RELAY_TTS_MODEL=your-model
EARSHOT_RELAY_TTS_VOICE=your-voice        # optional default
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

## The 700 ms lead-in

Phones often clip the first fraction of a second of audio while the audio route wakes up, which eats the start of a sentence. With `EARSHOT_RELAY_TTS_LEADIN_MS` above 0 (default 700) the relay runs ffmpeg once per message:

```
ffmpeg -i in -af adelay=700:all=1 -c:a libopus -b:a 48k out.opus
```

and uploads the result as `<channel>/<id>.opus` with `Content-Type: audio/ogg`. The container image includes ffmpeg. Outside Docker you need ffmpeg on the PATH, or set the lead-in to `0`, in which case the TTS output is uploaded unchanged as `.mp3` or `.wav`. The relay refuses to start if the lead-in is on and ffmpeg is missing.

## Failures never fall back

If the TTS server errors, returns a tiny or empty body, or times out, the request fails: `502 tts_failed` or `504 tts_timeout`. Nothing is written to the bucket. The relay does not quietly send the same text as a phone-voiced `.say`, because then you could not tell which voice you got, and text you meant to be synthesized would go out in a different form. If you want a fallback, do it in the caller: retry with `"speech": "device"`.

The TTS API key is never logged and never appears in error responses.
