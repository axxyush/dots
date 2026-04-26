## Wayfind (Agentverse)

Wayfind is a voice-first navigation agent for blind visitors inside a venue
whose floor plan was previously uploaded to SenseGrid. It pairs naturally
with ASI:One voice mode — the visitor speaks, ASI:One transcribes, Wayfind
answers in plain prose, and ASI:One reads the answer back aloud.

### How to use

1. The business owner runs SenseGrid, uploads a floor plan, pays, and
   receives a **venue ID** like `venue-7f2a01`. They print it on a QR code
   or NFC tag at the entrance.
2. The blind visitor opens ASI:One in voice mode and talks to Wayfind:
   - First message: the venue ID (scanned, pasted, or spoken).
   - Wayfind looks the venue up via SenseGrid's `wayfind-venue` protocol
     and confirms the room dimensions and entrance.
3. After that, the visitor can ask anything:
   *"How many exits are there?"* · *"Where is the counter?"* ·
   *"What's at my two o'clock?"*
4. To keep the position model accurate, the visitor narrates motion:
   *"I moved five steps forward"*, *"I turned ninety degrees right"*,
   *"I turned around"*. Wayfind dead-reckons the new pose deterministically
   (1 step ≈ 1 meter) before consulting the language model.

### Notes

- Wayfind runs locally and is reachable through the Agentverse mailbox.
- The visitor never sees the floor plan; SenseGrid owns it. Wayfind asks
  for it once per session via the agent-to-agent `VenueLookup` message.
- Replies are short (≤ 2 sentences) and free of markdown so they sound
  natural through TTS.

### Required env vars

- `ASI_API_KEY` — credential for the ASI:One LLM.
- `SENSEGRID_AGENT_ADDRESS` — the address SenseGrid prints on startup,
  used as the destination for `VenueLookup`.
- `WAYFIND_AGENT_SEED` — stable seed phrase for this agent's identity.
