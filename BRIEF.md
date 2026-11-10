<p align="center"><img src="media/LogoFull.svg" alt="Hunar.ai" height="40"></p>

<h1 align="center">AI Engineer Assignment: Voice + Chat Agent</h1>

## Overview

Build an AI agent that helps a user complete a task across two channels:

- **A web voice call** (a stand-in for a phone call)
- **A web chat** (a stand-in for a messaging app)

The conversation starts on a call. Some information is easy to give by talking; some is easier to type or upload. Your agent should move between the two naturally: ask for things in chat during the call, notice when they arrive, and follow up if something is still missing.

**How to use this repository: it is reference material only.** Copy or download what you need (such as the starter UI) into your own repository. Please do not fork it to send pull requests, and do not open issues or pull requests against it. Proprietary — see [LICENSE](LICENSE).

Use any tools you like, including AI coding assistants. You may use any language, framework, library, platform or architecture for the call, the chat, the agent, storage and anything else.

> [!CAUTION]
> **Strict requirement:** never expose secrets (API keys, tokens, credentials) to the browser or commit them to the repository. If the browser talks to a provider directly, use short-lived, scoped credentials issued by your server.

## The goal

Every session must end with all the collected information turned into a **single, sanitized, structured record** that is **ready to be sent to another service** (see [The final record](#the-final-record)).

Sanitized means every value is validated and normalized, conflicting values from the call and the chat are resolved, and nothing required is missing without the record saying so. Everything else in this brief serves this goal: the call and the chat are how you collect the information, and the record is what the business actually receives.

## Choose your scenario

Pick any use case that genuinely needs both channels. It must include:

- At least **3 pieces of information collected conversationally** on the call
- At least **3 pieces that are awkward by voice** and better collected in chat (for example an ID number, an email address, a photo or document, or a choice from a list)
- At least **1 optional** piece of information
- Clear rules for what counts as an **invalid** input

Examples (you are free to pick something else):

- A car insurance claim: what happened, where and when, and whether anyone was hurt on the call; policy number, vehicle and damage photos in chat.
- A clinic appointment: symptoms, how long they've lasted and a preferred slot on the call; patient ID, insurer from a list and a prescription photo in chat.
- A rental application: budget, move-in date and household size on the call; ID document, payslip and email address in chat.
- A delivery job application: work experience, preferred shift and area they can cover on the call; Aadhaar number, driving licence photo and bank IFSC code in chat.
- A bank account opening (KYC): purpose of the account, occupation and expected monthly deposits on the call; PAN number, address proof and email address in chat.
- A personal loan pre-qualification: loan amount, purpose and monthly income on the call; PAN number, last 3 months' bank statements and employer from a list in chat.
- A wrong or damaged order: what went wrong, when it arrived and whether they want a refund or replacement on the call; order ID, a photo of the item and the return pickup address in chat.
- A school admission enquiry: child's age, current school and reasons for switching on the call; child's name as on the birth certificate, birth certificate and class from a list in chat.
- A flight booking change: reason for the change, new preferred dates and flexibility on time on the call; booking reference (PNR), passenger names as on ID and a flight from a list of options in chat.
- A new electricity connection: type of property, expected load and when it's needed on the call; property ownership document, ID proof and a neighbour's consumer number in chat.
- A device repair claim: what's wrong with the device, when it started and how it's used on the call; serial number, purchase invoice and a service centre from a list in chat.
- A gym or course enrollment: goals, current fitness or skill level and preferred timings on the call; email address, a plan from a list and a health declaration form in chat.
- A home services booking: the problem (for example a leaking tap), how urgent it is and preferred time window on the call; exact address with landmark, photos of the problem and a slot from a list in chat.

## Your stack

You may use any voice stack or platform: hosted realtime APIs, open-source frameworks, voice agent platforms, or your own speech-to-text → LLM → text-to-speech pipeline. Any stack and architecture is fine, as long as no secrets reach the browser (see the strict requirement above).

In your README, explain your stack choice in 150 words or fewer, including what one session costs.

## Must-haves

1. **Web voice call.** A browser page where the user talks to the agent. The user must be able to interrupt the agent while it's speaking. The UI shows a best-effort live transcript of the call (both sides), so the user can follow what's going on alongside the chat inputs.
2. **Web chat.** A chat page tied to the same session as the call, where the agent sends messages and requests inputs (text values, choices, files) one at a time, and the user fills each one in and submits it.
3. **One conversation, two channels.** Both channels share the same state.
   - If the user submits something in chat while on the call, the call agent should know about it and acknowledge it at a natural moment.
   - If the call ends, the chat continues from where things left off.
4. **Produce the final record.** When a session reaches an end state, produce a single sanitized, structured record and deliver it (see [The final record](#the-final-record)). Its exact shape and format are up to you.
5. **Survives a restart.** Restarting your server must not lose sessions or collected data, and must not produce the final record twice.
6. **No leaked secrets.** No API keys, tokens or credentials in the browser or the repository (see the strict requirement in the overview). This applies to your deployment too: keep keys in server-side environment variables or a secret store.
7. **Deployed.** Reviewers won't run your code locally. Deploy your system at a public HTTPS URL (browsers only allow microphone access over HTTPS) that we can open and use, and keep it up through the review and interview period. Any host is fine, and free tiers are OK. Since the URL is public, you may cap usage or spend (for example, with a rate limit).

No visual polish or authentication is needed.

## The final record

1. **Define the record's shape.** You decide which fields your scenario's record contains, how each one is represented and the overall format. It doesn't need to match the example below or what the starter UI and its demo show; those are only illustrations. A JSON Schema (or equivalent) in your README is welcome.
2. **Deliver the record.** Delivery is deliberately simple. POST the record to a sample endpoint of your choice (for example, a stub you host or a request-bin style URL), or just log it to the console.
3. **Decide the outcome for every case.** At minimum:
   - `completed`: all required fields collected and valid
   - `partial`: the user stopped engaging (for example, they left or never came back) with required fields still missing or invalid
   - `optedOut`: the user explicitly asked to stop
   - `needsReview`: something your system shouldn't decide on its own (you define when)

    Document when each one is triggered, and what happens if the session changes after the record was produced (for example, a `partial` user comes back and finishes, and you produce a new `revision`).

Example record (illustrative only):

```json
{
  "sessionId": "s_7f3a",
  "revision": 1,
  "outcome": "partial",
  "outcomeReason": "user stopped responding with policyNumber missing",
  "fields": {
    "incidentDescription": {"value": "Rear-ended at a signal...", "source": "call", "collectedAt": "2026-11-05T10:02:11+05:30", "status": "valid"},
    "policyNumber": {"value": null, "source": null, "collectedAt": null, "status": "missing"},
    "damagePhotos": {"value": ["https://.../p1.jpg"], "source": "chat", "collectedAt": "2026-11-05T10:04:02+05:30", "status": "valid"}
  },
  "conflicts": [
    {"field": "pickupAddress", "callValue": "...", "chatValue": "...", "resolution": "chatValue", "reason": "typed value preferred over spoken"}
  ],
  "engagement": {"calls": 1, "callSeconds": 212, "chatMessages": 6},
  "costUsd": 0.041,
  "endedAt": "2026-11-06T09:00:12+05:30"
}
```

## Cost and latency

**Lower total inference cost per completed session is better**, as long as the conversation stays good. A comparison of approaches is welcome but not required.

In your README, report:

- **Cost per session**: what one completed session costs (voice, LLM and anything else), and how you worked it out.
- **Voice latency**: the time from the user stopping speaking to the agent starting to speak, as p50 and p95 over your test calls, and how you measured it.

## Bonus

Only attempt these once the must-haves work. They are only credited if done well.

- **Call drop recovery**: if the call drops unexpectedly (network loss, tab closed, server restart), the user can reconnect and the conversation resumes from where it was, without repeating collected information or losing the in-progress answer.
- **Conversation logging and debuggability**: it's easy to understand what happened in any session after the fact: the full transcript across both channels, what the agent decided and why, timings, costs and errors (for example, a timeline view or structured logs).
- **Language switching**: the conversation supports English, Hindi, and one other Indian language of your choice, including the user switching mid-conversation.
- **Resume by call from chat**: the user taps "call me back" in chat and the new call continues with full context.
- **Checking uploads**: validate uploaded photos or documents with a vision model.
- **HTTP access for testing**: documented endpoints to create a session, submit chat inputs as the user and see what happened in a session.
- **Good documentation**: a clear README that explains your architecture, design decisions and trade-offs, how to run it, known limitations and cost per call.

## Optional starter UI

![Starter UI demo: a voice call and chat collecting a claim, ending in the final record](media/demo.gif)

*The optional starter UI running its scripted demo (about 2 min, no audio). Click for the full-quality video.*

To save you browser audio work, the [`starter/`](starter/) folder contains a small browser UI (React + TypeScript, `starter/src/`; see [`starter/README.md`](starter/README.md) to run it). It is a single page with:

- A call button in the chat header: well-configured microphone capture (PCM16 mono 16 kHz), gapless playback of PCM16 audio, and a way to stop playback immediately when the user interrupts, wired to a WebSocket
- A single conversation thread below it: the live call transcript (both sides, updated as speech is recognized) interleaved with input cards the agent sends (text, choices, files and so on), shown one at a time; each submitted input becomes a chat bubble
- Optionally, a preview of the session's final record at the end of the thread

The suggested message formats, including the transcript and final record messages, are typed in [`starter/src/protocol/`](starter/src/protocol/); the record shown there is only an example. Open the page with `?demo=1` to see the intended flow without a backend.

There is no backend in it; the backend is entirely yours. The starter is optional: you can use it as is, modify it (including the protocol and how it connects to providers) or build your own UI from scratch, as long as no secrets reach the browser. We won't score you higher or lower for that choice; we evaluate the system you build, not whether you used the starter.

## What to submit

- The **public HTTPS URL** of your deployed system
- Your own git repository (not a fork of, or pull request against, this one) containing:
  - The code, with brief setup instructions so it could be run locally
  - A `README.md` covering:
    - Your architecture, and how state is shared between the two channels
    - How the final record is built and sanitized: validation, normalization and conflict resolution
    - When each outcome is triggered, how you deliver the record, and when you produce a new revision
    - Your stack choice and cost per session (150 words or fewer)
    - The product decisions you made where this brief was vague, and why
    - Edge cases you handled, and ones you know you didn't
    - What in your own system you trust least, and why
    - Cost and latency, as described above
    - What you would do next with another week
    - If you used AI tools: where they helped and where they misled you
- Optional: a short screen recording of one full session
