# DIGITS DaaS API — Endpoint Audit Findings

Authorized security audit of every DIGITS desktop-app API surface not
exercised by the base CLI. All tests were run against the researcher's
own account/line with the CLI's own registered device, REST-only (no
WebSocket notification-channel connects), September 2026.

Request constructions are derived from the beautified renderer bundle
(`/tmp/chunk_ab`, decompiled from the DIGITS desktop app); line citations
appear per row. Raw request/response evidence for every live test is in
`audit_evidence/` (local only — git-ignored, never committed).

Status legend:

- **VERIFIED-LIVE** — exercised against the live backend, real status + body
- **REACHABLE-UNVERIFIED** — route deployed + auth accepted, but the
  operation could not be completed (destructive, needs a live object, or
  backend rejected the probe payload)
- **NOT-REACHABLE** — route not deployed on this environment (or the app
  itself stubs it out)
- **DOCUMENTED-ONLY** — surface exists in code; deliberately never invoked

Common request frame (every call): headers from `lib/config.py`
`config.headers(access_token)` — `daasAccessToken`, `device_id`,
`unique-session-number`, `client-type: cDigits`, `daasClientId: DAAS_WEB`,
`x-correlation-id`/`trxId` (fresh UUID), `X-Mav-Client-Version:
DesktopApp_27.0.0_NA_DIGITS_2.5.19_Production` — matching the app's
`daasFetchWorker` (chunk_ab 5994-5997). There is **no client attestation,
mTLS, or signature** anywhere in this header set: a stolen access token
(stored unencrypted by the desktop app) is sufficient for every row below.

## Summary table

| # | Endpoint | Method | Status | Evidence |
|---|---|---|---|---|
| 1 | `daasvmsvc/voicemails` | GET | VERIFIED-LIVE | 200 |
| 2 | `daasvmsvc/voicemail/quota` | GET | VERIFIED-LIVE | 200 |
| 3 | `daasvmsvc/voicemail/flag` | PUT | VERIFIED-LIVE | 200 |
| 4 | `daasvmsvc/voicemail/flag/{id}` | DELETE | NOT-REACHABLE | 404 ERR-1004 |
| 5 | `daasvmsvc/voicemail/{id}` | DELETE | VERIFIED-LIVE | 200 |
| 6 | `daasvmsvc/voicemail/transcript` | PUT | VERIFIED-LIVE | 202 |
| 7 | `daasvmsvc/voicemail/greeting` | GET/PUT | NOT-REACHABLE | 404 ERR-1004 |
| 8 | `daasvmsvc/greeting/flag/{id}` | DELETE | NOT-REACHABLE | 404 ERR-1004 |
| 9 | `caas/contacts/search` | GET | VERIFIED-LIVE | 200 |
| 10 | `caas/contacts/searchXdms` | GET | REACHABLE-UNVERIFIED | 500 |
| 11 | `caas/contacts` (create) | POST | REACHABLE-UNVERIFIED | 405 |
| 12 | `caas/contacts/thumbnail` | GET | REACHABLE-UNVERIFIED | 500 |
| 13 | `digitslink/v1/userlinkinfo` | GET | VERIFIED-LIVE | 200 |
| 14 | `digitslink/api/unlink` | POST | REACHABLE-UNVERIFIED | 500 |
| 15 | `daas/contactCapabilities` (single) | GET | VERIFIED-LIVE | 200 |
| 16 | `daas/contactCapabilities` (isList bulk) | POST | NOT-REACHABLE | 404 |
| 17 | `daas/call/shortcode` (USSD) | POST | VERIFIED-LIVE | 201 |
| 18 | `chat/pager_large_message_mode` | POST | VERIFIED-LIVE | 201 |
| 19 | `filetransfer/sessions` | POST | VERIFIED-LIVE | 201 |
| 20 | `filetransfer/download` | GET | REACHABLE-UNVERIFIED | 400/500 |
| 21 | `chat/messages/status` (receipts) | POST | VERIFIED-LIVE | 204 |
| 22 | `chat/isComposing` | POST | VERIFIED-LIVE | 201 |
| 23 | `chat/sessions` (group) | POST | VERIFIED-LIVE | 201 |
| 24 | `daas/groupchat/rejoin` | POST | VERIFIED-LIVE | 201 |
| 25 | `daas/mstore_objects/bulkUpdate` | POST | REACHABLE-UNVERIFIED | 404 |
| 26 | `daas/mstore_objects/delete` | POST | REACHABLE-UNVERIFIED | 500 |
| 27 | `daas/emergency/chat/sessions` (SOS) | POST | DOCUMENTED-ONLY | — |
| 28 | `digitsOrchestratorService/logout` | DELETE | REACHABLE-UNVERIFIED | — |
| 29 | `sendLocation` | POST | NOT-REACHABLE (app stub) | — |
| 30 | voicemail greeting upload/PIN APIs | * | NOT-REACHABLE (app stub) | — |

---

## 1. Voicemail service (`https://cpaas-geo.t-mobile.com/v1/daasvmsvc`)

Base derived at chunk_ab 7912-7914 (`initialize`: `voicemailUrl =
${origin}/v1/daasvmsvc`). Every call carries headers `client-id:
RkdOw9xHHfMLGKaTHRbTuzExZYngWBxm` (hardcoded in the bundle, chunk_ab 7922),
`client-version`, and `msisdn` — where **msisdn must be the `tel:+1…` URI
form** (`formatPhoneNumbers`, chunk_ab 8459; the bare 10-digit form returns
`401 VVM-UNAUTHORIZED Invalid msisdn`, confirmed live).

### 1.1 GET /voicemails — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7945-7953 (`getVoicemails`)
- **Request**: `GET /v1/daasvmsvc/voicemails?audiotype=audio/mp4&deltaSyncFromDate=2022-01-01T00:00:00.000Z&count=20&offset=0` + vm headers
- **Live result**: `200` — full mailbox listing: per message `objectid`,
  caller's number (`fromnumber`), date, duration, expiry date, read flag,
  transcript flag, and (with `audiotype=audio/mp3`) the base64 audio
  payload URL. 3 entries returned on the test line.
- **Evidence**: `audit_evidence/001_*_vm-list.txt`
- **Security significance**: a stolen token dumps the owner's entire
  voicemail mailbox — who called, when, how long, and the audio
  itself — with zero additional auth. The mailbox is read-adjacent to
  voicemail **transcription** (1.4), i.e. message *content*, not just
  metadata.

### 1.2 GET /voicemail/quota — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7970-7979 (`getVoicemailQuota`)
- **Request**: `GET /v1/daasvmsvc/voicemail/quota` + vm headers
- **Live result**: `200` — `{"quota":{"currentobjectcount":3,"maxallowedstorage":…,"maxnumberofallowedobjects":…,"utilizedstorage":…}}`
- **Evidence**: `audit_evidence/002_*_vm-quota.txt`
- **Security significance**: trivial, but confirms the service scopes to
  the token's line with no per-user challenge.

### 1.3 PUT /voicemail/flag — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7919-7934 (`updateFlag`), called from
  `updateVoicemailFlag` chunk_ab 8520-8522 (mark Seen) and
  `updateGreetingFlag` chunk_ab 8539-8540
- **Request**: `PUT /v1/daasvmsvc/voicemail/flag` with body
  `{"voicemails":[{"flag":"Seen","objectid":"<id>"}]}` + vm headers
- **Live result**: `200` `{}` (marked own VM seen; idempotent)
- **Evidence**: `audit_evidence/001_*_vm-flag-seen.txt`
- **Security significance**: lets a token holder tamper with the read
  state of the owner's mailbox (hide that a voicemail is unheard).

### 1.4 PUT /voicemail/transcript — VERIFIED-LIVE (202)

- **Source**: chunk_ab 7989-8002 (`setTranscript`), wrapper chunk_ab 8536-8538
- **Request**: `PUT /v1/daasvmsvc/voicemail/transcript` with body
  `{"language":"en","objectId":"<id>"}`
- **Live result**: `202 Accepted` (transcription job queued server-side)
- **Evidence**: `audit_evidence/004_*_vm-transcript.txt`
- **Security significance**: a token holder can order the carrier to
  transcribe any voicemail in the box and then read the transcript via
  1.1 — i.e. **convert stored voice content to text** for exfiltration,
  cheaply and at scale.

### 1.5 DELETE /voicemail/{id} — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7960-7969 (`deleteVoicemail`)
- **Request**: `DELETE /v1/daasvmsvc/voicemail/<id>` + vm headers
- **Live result**: `200` with empty body — including for a **nonexistent**
  id (idempotent delete). Tested with a nonexistent id only; no real
  voicemails were destroyed.
- **Evidence**: `audit_evidence/005_*_vm-delete-fake-id.txt`
- **Security significance**: a stolen token can **destroy evidence** —
  wipe voicemails (e.g. a voicemail containing a threat or a 2FA code)
  from the owner's mailbox. No confirmation challenge of any kind.

### 1.6 DELETE /voicemail/flag/{id} — NOT-REACHABLE (404)

- **Source**: chunk_ab 7935-7944 (`deleteFlag`) + 7956-7959
  (`deleteVoicemailFlag` — the app's mark-*Unseen* path)
- **Request**: `DELETE /v1/daasvmsvc/voicemail/flag/<id>`
- **Live result**: `404 {"reasonCode":"ERR-1004"…}` — route not deployed
  (mark-unseen is not reachable in this environment; only PUT Seen works)
- **Evidence**: `audit_evidence/002_*_vm-flag-delete.txt`

### 1.7 GET/PUT /voicemail/greeting — NOT-REACHABLE (404)

- **Source**: no GET/PUT greeting call exists in the bundle — greetings
  ride the `/voicemails` listing (chunk_ac 5476-5521 shows the greeting
  entries consumed from the VM store); `uploadGreeting` /
  `setVoicemailGreeting` are stubs (chunk_ab 7985-7986)
- **Request**: `GET|PUT /v1/daasvmsvc/voicemail/greeting`
- **Live result**: `404 {"reasonCode":"ERR-1004","systemMessage":"Not Found"}` —
  tried GET, PUT with `{"voicemails":[…]}` body, and PUT with
  `{"greeting":{…}}` body; all 404
- **Evidence**: `audit_evidence/003_*_vm-greeting-get.txt`,
  `audit_evidence/001|002_*_vm-greeting-put-probe.txt`

### 1.8 DELETE /greeting/flag/{id} — NOT-REACHABLE (404)

- **Source**: chunk_ab 7981-7984 (`deleteGreetingFlag`)
- **Request**: `DELETE /v1/daasvmsvc/greeting/flag/<id>`
- **Live result**: `404 ERR-1004` (nonexistent id; no greetings exist on
  the test line so a valid id was unavailable — treated as not deployed
  given ERR-1004 is the same route-level 404 the greeting PUT produced)
- **Evidence**: `audit_evidence/006_*_vm-greeting-flag-delete-fake.txt`

### 1.9 Greeting/PIN management — NOT-REACHABLE (app stubs)

- **Source**: chunk_ab 7985-7988 — `uploadGreeting`,
  `setVoicemailGreeting`, `setVoicemailPIN`, `updateVoicemailPin` are all
  `Promise.reject(de("N/I", 404))` **in the app itself**: the desktop app
  never implements them; the settings UI records greetings out-of-band
- **Security significance**: n/a — dead surface, documented for
  completeness

## 2. Contacts (`caas`) — base `https://cpaas-geo.t-mobile.com/v1/digitsApi`

The `caas` family hangs off the **digitsApi root**, *not* the
orchestrator (ContactsService initializes `daasContactsAPIs` with
`cfg.daasRootUrl`, chunk_ab 7601 + 7704; `daasRootUrl` =
`envConfig.daasURLBase` = `https://cpaas-geo.t-mobile.com/v1/digitsApi`,
chunk_ac 12948-12973 and asar `node_modules/digits-common/build/constants/configuration.js`).
This is why naive `{orchestrator}/caas/...` probes 404.

### 2.1 GET /caas/contacts/search — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7520-7528 (`fetchContactsPage`)
- **Request**: `GET /v1/digitsApi/caas/contacts/search?pageNo=1&pageSize=200&all=true` + standard headers
- **Live result**: `200` — `{"results":[{"totalContacts":0,…,"contacts":[…]}]}`
  (0 contacts on this account; the account owner never synced any)
- **Evidence**: `audit_evidence/004_*_contacts-search.txt`
- **Security significance**: the DIGITS-native contact store is
  account-wide. For a user who stores contacts there, a stolen token
  exfiltrates the full address book (names, numbers, emails) in pages of
  200. Also the write path (2.3) means an attacker can **poison** the
  store.

### 2.2 GET /caas/contacts/searchXdms — REACHABLE-UNVERIFIED (500)

- **Source**: chunk_ab 7520-7528 (`fetchContactsPage(…, xdms=true)`)
- **Request**: `GET /v1/digitsApi/caas/contacts/searchXdms?pageNo=1&pageSize=200&all=true`
- **Live result**: `500 {"status":"failure","errorDescription":"Internal
  Server Error","errorDetail":"Error occurred while searching for the
  glo[bal]…}` — route deployed and token-accepted; the XDMSServer query
  fails when no Google/Microsoft account is linked
- **Evidence**: `audit_evidence/005_*_contacts-search-xdms.txt`
- **Security significance**: this is the surface that pulls **contacts
  synced from a linked Google or Microsoft account** into DIGITS. On an
  account with a link established, the same endpoint returns the linked
  provider's contact data — one more exfiltration path for a token thief.

### 2.3 POST /caas/contacts (createContact) — REACHABLE-UNVERIFIED (405)

- **Source**: chunk_ab 7529-7534 (`createContact` → `daasPostFetch(t, {}, e)`)
- **Request**: `POST /v1/digitsApi/caas/contacts` with contact JSON body
- **Live result**: `405 {"title":"Method Not Allowed","detail":"Method
  'POST' is not supported."}` — route exists (server answers with a
  method-level 405, not a route 404); POST is disabled in this
  environment. No contact was created.
- **Evidence**: `audit_evidence/001_*_contacts-create-probe.txt`
- **Security significance**: if enabled anywhere, this is a
  store-poisoning primitive (inject fake contacts into the owner's
  DIGITS client).

### 2.4 GET /caas/contacts/thumbnail — REACHABLE-UNVERIFIED (500) — token in URL

- **Source**: chunk_ab 7552-7556 (`getDaasContactThumbnailUrl`)
- **Request**: `GET /v1/digitsApi/caas/contacts/thumbnail?photoId=<id>&daasAccessToken=<TOKEN>&device_id=<id>&client-type=cDigits`
  — **the access token is passed as a query parameter**
- **Live result**: `500 {"errorDetail":"Error occurred while getting
  contact image…"}` — route deployed; fails on a nonexistent photoId. No
  contact thumbnails exist on this account.
- **Evidence**: `audit_evidence/010_*_contacts-thumbnail.txt`
- **Security significance**: **tokens leak into URLs** — server logs,
  proxies, and browser history at T-Mobile's edge (Akamai fronts this
  host) capture `daasAccessToken` in cleartext query strings for every
  thumbnail the app fetches. This is a distinct token-disclosure
  vulnerability beyond the unencrypted token storage.

## 3. Account linking (`digitslink`)

### 3.1 GET /digitslink/v1/userlinkinfo — VERIFIED-LIVE (200)

- **Source**: chunk_ab 7535-7540 (`getLinkedAccounts`)
- **Request**: `GET /v1/digitsApi/digitslink/v1/userlinkinfo` + standard headers
- **Live result**: `200 {"status":"No accounts linked to this account."}`
- **Evidence**: `audit_evidence/006_*_digitslink-userlinkinfo.txt`
- **Security significance**: enumerates which Google/Microsoft identities
  are linked to the DIGITS account (account-recon for phishing);
  combined with 3.2 it's the prelude to hijacking those links.

### 3.2 POST /digitslink/api/unlink — REACHABLE-UNVERIFIED (500)

- **Source**: chunk_ab 7541-7551 (`unlinkContact`) — form body
  `fed_type=…&fed_email=…`
- **Request**: `POST /v1/digitsApi/digitslink/api/unlink`,
  `Content-type: application/x-www-form-urlencoded`
- **Live result**: `500 "Content-Type '…' is not supported"` for
  urlencoded (with and without charset) **and** for JSON — the route is
  deployed and authenticated but rejects every probe content-type the
  bundle itself would send. Nothing was unlinked (no accounts are linked).
- **Evidence**: `audit_evidence/002_*_digitslink-unlink-probe*.txt`,
  `audit_evidence/001|002_*_digitslink-unlink-json-probe.txt`
- **Security significance**: if reachable with the right framing, a
  stolen token can **sever the owner's Google/Microsoft links** —
  account-takeover prep (unlink, then relink attacker-controlled
  identity).

## 4. Capability discovery

### 4.1 GET /daas/contactCapabilities (single) — VERIFIED-LIVE (200)

- **Source**: chunk_ab 12042-12066 (`getSingleLineCapabilities`) —
  headers `isList=false`, `to=<number>`
- **Request**: `GET …/digitsOrchestratorService/daas/contactCapabilities`
  with `isList: false`, `to: <10-digit>` headers
- **Live result**: `200` — `{"contactServiceCapabilities":{"contactId":…,
  "resourceURL":"https://wrgcore.cnf.phi401.sip.t-mobile.com/capabilitydiscovery/v1/…",
  "serviceCapability":[…capabilityId/status list…]}}` — per-number RCS
  capability (chat, file transfer, standalone messaging, OGC)
- **Evidence**: `audit_evidence/007_*_capabilities-single.txt`
- **Security significance**: an **oracle for carrier subscriber
  enumeration**: with a stolen token you can probe arbitrary phone
  numbers and learn which are T-Mobile RCS-capable (i.e. on-network
  T-Mobile subscribers) — a carrier-account-existence oracle.

### 4.2 POST /daas/contactCapabilities (isList bulk) — NOT-REACHABLE (404)

- **Source**: chunk_ab 12067-12084 (`getMultipleLineCapabilities`) —
  body `{"adhocContactList":{"contactId":["tel:…",…}}`, header
  `isList=true`. Callers: only `allPartiesAreOGCCapable` (chunk_ab
  14275-14279), which itself has no call sites — dead code in the app.
- **Request**: POST with both `tel:+1…` and `tel:1…` contactId formats
- **Live result**: `404 ERR-1004` for both formats (GET on the same URL
  returns 200, so the POST route simply isn't deployed)
- **Evidence**: `audit_evidence/008_*_capabilities-bulk.txt`,
  `audit_evidence/001|002_*_capabilities-bulk-tel*.txt`
- **Security significance**: had it been live, this would have been the
  bulk-subscriber-enumeration oracle (whole number ranges in one call).

## 5. USSD

### 5.1 POST /daas/call/shortcode — VERIFIED-LIVE (201)

- **Source**: chunk_ab 9875-9909 (`sendUSSDCodeStart`). Note the URL
  construction: `daasCallUrl.split("/")` with `c[4]="digitsApi"` — the
  join yields `…/digitsApi/digitsOrchestratorService/daas/call/shortcode`
  (the `c[4]` assignment is a no-op on the production base). Body:
  `ussdSessionInformation` with XML `ussdBlob`
  `<ussd-data><language>en</language><ussd-string>…</ussd-string></ussd-data>`,
  `sdp` (in the app, the established USSD call's SDP), and
  `clientCorrelator`.
- **Request**: `POST …/digitsOrchestratorService/daas/call/shortcode`
  with `{"ussdSessionInformation":{…}}` (sdp left empty — no call leg)
- **Live result**: `201 Created` — full `ussdSessionInformation` echo
  with a wrgcore resourceURL, for the `*#06#` IMEI-display code
- **Evidence**: `audit_evidence/001_*_ussd-shortcode.txt`
  (an earlier probe at `/v1/digitsApi/shortcode` — wrong base — 404'd,
  `audit_evidence/003_*_ussd-shortcode.txt`)
- **Security significance**: USSD works **without any call leg or SDP** —
  the endpoint accepts an empty `sdp` and still creates the session.
  USSD is the channel for carrier feature codes (call-forwarding
  activation, balance, IMEI display…): a stolen token can send USSD
  codes on the owner's line. Combined with call-forwarding codes this
  is a **call-interception primitive** (forward the owner's calls to
  an attacker number) that needs no voice call and no WS channel.

## 6. Large-message mode

### 6.1 POST /chat/pager_large_message_mode — VERIFIED-LIVE (201)

- **Source**: chunk_ab 12229-12269 (`sendLargeModeMessage`) — headers
  `from=<1+number>`, `mode: large_msg|pager` (chosen by
  `bodyText.length > 1300`, chunk_ab 12234-12237), `to=<number>`; body
  `outboundMessageRequest.outboundIMMessage.{bodyText, subject,
  imFormat: IMLargeMode|IMPagerMode}` + `reportRequest
  ["DeliveredToTerminal","Displayed"]`
- **Request**: 1,764-char bodyText (forces `imFormat: IMLargeMode`,
  `mode: large_msg`) to the researcher's own second phone
- **Live result**: `201 Created` — `outboundMessageRequest.resourceURL`
  returned; message delivered
- **Evidence**: `audit_evidence/001_*_pager-large-message-mode.txt`
- **Security significance**: bypasses the normal per-session message
  path (no `conv-session-id` needed!) — the token alone is enough to
  push arbitrarily long messages to any address, i.e. a spam/SMishing
  primitive on the owner's line (messages originate from the owner's
  number).

## 7. File transfer

### 7.1 POST /daas/filetransfer/sessions — VERIFIED-LIVE (201)

- **Source**: chunk_ab 11895-11965 (`sendFile`) + the multipart framer
  `pf()` chunk_ab 4523-4547. Quirks reproduced exactly: outer
  `content-type: text/plain` with the true type in
  `Wrg-Content-Type: multipart/form-data; charset=utf-8;
  boundary="===123456==="` (literal boundary), body = `--===123456===`
  part `root-fields` (JSON `fileTransferSessionInformation` with sha-1
  hash, size, name, type) + nested `attachments` part with
  base64-encoded payload; headers `to`, `group`, `x-p-associated-from`.
- **Request**: 64-byte text file to the researcher's own second phone
- **Live result**: `201 Created` — `fileTransferSessionInformation` with
  wrgcore `resourceURL` and imdn id; the file transfer session was
  established end-to-end
- **Evidence**: `audit_evidence/001_*_filetransfer-sessions.txt`
- **Security significance**: any binary content can be pushed from the
  owner's number to any recipient (SMishing attachment primitive), and
  the framing quirk (fake text/plain outer type) is exactly the kind of
  thing a WAF misses — the request sails through Akamai unscanned.

### 7.2 GET /daas/filetransfer/download — REACHABLE-UNVERIFIED (400/500)

- **Source**: chunk_ab 12085-12112 (`downloadFile`) — GET with headers
  `accept`, `blob: true|false`, `content-type`, `line`, `resourceUrl`
  (the resource URL passed as a HEADER, not a query param)
- **Request**: probe with malformed resourceUrl → `400
  {"status":"The ResourceUrl is in an improper format, cannot determine
  target"}` (route + header parsing live); with the real 7.1 session's
  resourceURL → `500` (byte flow requires the receiving side's WS accept
  handshake per the app's flow — not exercised REST-only)
- **Evidence**: `audit_evidence/001|002_*_filetransfer-download.txt`,
  `audit_evidence/009_*_filetransfer-download.txt`
- **Security significance**: paired with a resource URL (which a
  MITM/WS-observer could harvest), this is the download side of the
  exfiltration channel.

## 8. RCS chat extras

### 8.1 POST /chat/messages/status — VERIFIED-LIVE (204) — forged receipts

- **Source**: chunk_ab 12194-12212 (`sendMessageDeliveredOrDisplayed`) —
  body `{"messageStatusReport":{"status":"Delivered|Displayed",
  "senderAddress":…, "receiverAddress":…}}`, headers
  `conv-session-id`, `group`, `imdn-msg-id`, `to`. The sender/receiver
  addresses are **client-supplied strings** — the server does not bind
  them to the session or the token.
- **Request**: forged `Displayed` receipt for a message this CLI itself
  sent, over the real 1:1 session
- **Live result**: `204 No Content` — receipt accepted
- **Evidence**: `audit_evidence/003_*_messages-status-receipt.txt`
- **Security significance**: **arbitrary-receipt forgery**. A token
  holder can make the owner's phone appear to have read/delivered
  messages it never saw — an anti-forensics primitive (fake an alibi in
  chat: "the message was Displayed") and a way to suppress the sender's
  retry/read-confirmation UI. The server trusts client-declared
  addresses completely.

### 8.2 POST /chat/isComposing — VERIFIED-LIVE (201)

- **Source**: chunk_ab 11966-11983 (`sendIsComposing`) — body
  `{"isComposing":{"contenttype":"text/plain","refresh":"7",
  "state":"active"}}`, headers `conv-session-id`, `group`
- **Request**: typing indicator on the live 1:1 session
- **Live result**: `201 Created` — `{"isComposing":{"refresh":7,"state":"active"}}`
- **Evidence**: `audit_evidence/004_*_iscomposing.txt`
- **Security significance**: social-engineering assist (impersonate the
  owner "typing…" before a phishing message lands).

### 8.3 POST /chat/sessions group=true (startGroupChat) — VERIFIED-LIVE (201)

- **Source**: chunk_ab 11740-11805 (`startGroupChat`) — body
  `groupChatSessionInformation.{clientCorrelator, isClosed,
  participant[], subject}`; first participant is originator
  (`isOriginator: true`); headers `group: true`, `to` (comma-joined
  recipients)
- **Request**: group chat with the researcher's own second phone
- **Live result**: `201 Created` — `groupChatSessionInformation` with
  SIP-form participant addresses and wrgcore resourceURL
- **Evidence**: `audit_evidence/001_*_groupchat-start.txt`
- **Security significance**: silent group-chat injection — an attacker
  can spin up group conversations on the owner's line (group phishing /
  thread hijacking).

### 8.4 POST /daas/groupchat/rejoin — VERIFIED-LIVE (201)

- **Source**: chunk_ab 11984-12041 (`rejoinGroupChat`) — body
  `{"participantList":{"participant":[{address: "1…", name,
  clientCorrelator}…]}}`, header `conv-session-id`
- **Request**: rejoin against the group session created in 8.3
- **Live result**: `201 Created` — fresh `groupChatSessionInformation`
- **Evidence**: `audit_evidence/002_*_groupchat-rejoin.txt`
- **Security significance**: lets a token holder **re-insert itself
  into an existing group conversation** (session hijack/re-entry into
  threads the owner belongs to).

### 8.5 sendLocation — NOT-REACHABLE (app stub)

- **Source**: chunk_ab 12225-12228 — the API-layer `sendLocation` is
  literally `Promise.resolve({resourceURL: e, status: "delivered"})` —
  **no network call exists**; the service-layer wrapper (chunk_ab
  13961-13973, 14691-14697) only forwards into that stub
- **Security significance**: none — dead surface in this build
  (location sharing is evidently not implemented for the RCS path).

## 9. Message store tampering (`mstore_objects`)

Both routes live under `…/digitsOrchestratorService/daas/mstore_objects`.
Object ids are the `nms/v1/myStore` resourceURL tails (chunk_ab 5466,
 13803-13804) — the store is populated via WS notifications, so a
REST-only audit could not obtain a valid id of even its own sent
messages (sync probes: `syncthreads` 200-empty, `syncmessages` 502 —
evidence `001|002_*_syncmessages-*.txt`, `001_*_syncthreads*.txt`).
Per the audit rules, destructive verification against store objects was
skipped; both routes were probed for deployment + auth only.

### 9.1 POST /daas/mstore_objects/bulkUpdate — REACHABLE-UNVERIFIED (404)

- **Source**: chunk_ab 12213-12224 (`bulkUpdateEntriesReadStatus`) —
  headers `line`, `read: "true"|"false"`, body `{"objectIds":[…]}`
  (batched ≤100 per call by the caller, chunk_ab 14603-14611)
- **Live result**: `404 {"returnCode":"404","status":"404 from store"}`
  — authenticated, routed into the store backend, which rejected the
  (nonexistent) id
- **Evidence**: `audit_evidence/005_*_mstore-bulkupdate.txt`
- **Security significance**: with real ids, this flips read/unread
  state on arbitrary messages in the owner's sync store (hide unread
  messages from the owner — a message-suppression primitive).

### 9.2 POST /daas/mstore_objects/delete — REACHABLE-UNVERIFIED (500)

- **Source**: chunk_ab 12179-12193 (`deleteChannelEntries`) — headers
  `line`, body `{"objectIds":[…]}`; ids are stripped of the `_webgw_`
  suffix before sending
- **Live result**: `500 {"returnCode":"500","status":"500
  INTERNAL_SERVER_ERROR"}` — authenticated, routed into the store
  backend (which errored on the nonexistent id)
- **Evidence**: `audit_evidence/006_*_mstore-delete.txt`
- **Security significance**: with real ids, a stolen token can **delete
  the owner's message history server-side** — evidence destruction at
  the carrier's sync store, not just the local app.

## 10. Emergency (SOS) — DOCUMENTED-ONLY, never invoked

- **Source**: chunk_ab 11637-11714 (`setupEmergencyChat`):
  `POST …/daas/emergency/chat/sessions` with body
  `chatSessionInformation.{clientCorrelator, originatorAddress,
  originatorName, tParticipantAddress: "urn:service:sos.messaging",
  tParticipantName: "SOS"}`; headers `group: false`, `to: 911|922`; with
  a geolocation PIDF-LO body it goes out as multipart with
  `Geolocation-Routing: yes` + `Geolocation: <cid:…>` headers (chunk_ab
  11656-11666). The call-side SOS path is `startEmergencyCall` (chunk_ab
  9416-9420, 10474-10477) targeting `urn:service:sos`.
- **Live result**: deliberately **not tested** — no emergency surface
  was ever invoked in this audit
- **Security significance**: the code path shows an unauthenticated
  (token-only, no separate emergency credential) route to creating SOS
  sessions — a swatting/false-emergency primitive if abused, which is
  precisely why it was never exercised. Documented for the defenders'
  threat model only.

## 11. Session teardown

### 11.1 DELETE /digitsOrchestratorService/logout — REACHABLE-UNVERIFIED

- **Source**: chunk_ab 5955-5961 (`logout`) — `DELETE
  {daasRegBaseUrl}/logout` with header `cdrInfo: daasreg`
- **Request shape**: fully derived (above); **invocation deliberately
  deferred** — it deregisters the DaaS registration for the device
  identity in `device.json`, which is shared with other live tooling on
  this line during the audit
- **Security significance**: this is a **denial-of-service surface for the
  victim's active devices**: a stolen token can DEREGISTER the victim's
  live DIGITS registrations (phone/tablet/desktop), knocking their
  inbound text/call service offline until they re-register — from the
  victim's perspective, DIGITS mysteriously stops receiving anything.
  Whatever a token can create it can also tear down. Method/headers are
  wired into `tmo logout` for whenever it's wanted.

## Cross-cutting observations

1. **No attestation anywhere.** Every surface above accepts the same
   `daasAccessToken` header set; there is no per-scope authorization, no
   client proof, no device binding beyond the header strings themselves
   (which are client-supplied and were replayed from a non-T-Mobile
   client without modification).
2. **Client-declared identity is trusted.** `senderAddress`,
   `receiverAddress`, `from`, `to`, `line`, `msisdn` are all headers or
   body fields the client chooses (see the forged-receipt 204 in 8.1 —
   the server accepted a receipt for addresses it never verified).
3. **Token leaks into URLs.** The thumbnail endpoint (2.4) puts
   `daasAccessToken` in a query string — logged by every proxy/CDN in
   path.
4. **Framing tricks defeat WAF inspection.** File transfer (7.1)
   declares `content-type: text/plain` while shipping a multipart
   payload via a non-standard `Wrg-Content-Type` header — middleboxes
   see text, not an attachment.
5. **Destructive primitives are confirmation-free.** Voicemail delete
   (1.5) and store delete (9.2) take a bare id with no challenge;
   receipt forgery (8.1) and unlink (3.2) likewise.
6. **Every REST surface generates push traffic on the notification
   channel.** During this audit, a live WS notification listener
   attached to the same device registration captured push events for
   every REST-triggered operation — voicemail pushes, the USSD session,
   the file-transfer session, the group-chat session, and the chat
   receipts — independently confirming (a) each of these API surfaces
   emits observable notification traffic (a network-observer can infer
   victim activity from push volume/patterns), and (b) WRG notification
   routing follows the most-recently-connected channel per device, which
   is itself an attack-surface primitive: whoever connects last receives
   the victim's notifications (see the parallel WS-channel study in this
   engagement).

## Reproducing

All endpoints above are wired into the CLI (`tmo audit` runs the
read-only battery and prints the table; individual commands per
`README.md`). Evidence files are written to `audit_evidence/` on every
call. Use only on your own account.
