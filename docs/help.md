# tmo help — command reference

```
tmo — headless CLI for DIGITS from T-Mobile

USAGE
  tmo COMMAND [OPTIONS]

COMMANDS
  login [--username N]         browser OAuth login (password + SMS 2FA)
                              the ONLY credential path — no REST refresh
  register                    establish session + register this device
  status                      device identity, line, token state

  sms send --to N --text T    send a text (RCS when recipient is capable)
  threads                     conversation threads (device-scoped)
  calllogs                    call history (line-scoped, full)
  e911                        E911 address on file

  listen                      live monitor of every notification event

  respond                     AI auto-responder: inbound texts from
                              TMO_RESPOND_TO answered by gpt-5.4-mini
                              (mission prompt: lib/respond_persona.txt)

  call --target N [--hold S]  place a voice call          [voice extra]
  ai-call --target N [--max S]
                              AI voice agent (GPT-Live)  [voice+ai extras]

AUTH
  Browser-login-only by design: `tmo login` opens your browser at
  T-Mobile ID; complete password + 2FA; the OAuth redirect is captured
  at localhost:8080 and exchanged for tokens. Access tokens live ~24h —
  when one expires, login again. The REST refresh path is intentionally
  absent: scripted token rotation is bot-shaped and trips carrier
  bot/fraud detection.

FILES
  .env          TMOBILE_USERNAME, TMOBILE_PASSWORD, OAI_API_KEY,
                TMO_RESPOND_TO (never committed)
  tokens.json   DaaS tokens from the last login
  device.json   this install's device identity

INSTALL
  pip install git+https://github.com/TravelMaxx/tmo-cli.git
  # or
  curl -fsSL https://raw.githubusercontent.com/TravelMaxx/tmo-cli/main/quick-install.sh | bash

EXAMPLES
  tmo login && tmo register && tmo status
  tmo sms send --to 5551234567 --text "hello"
  tmo calllogs
  tmo respond     # then text the line from the allowlisted number
```
