# Field transcripts

Real chats with the app, kept as examples of how athletes actually write. They are the first source of eval cases ([ADR-0034](../../docs/adr/0034-lean-evaluation.md)): synthetic cases come second, for a bug or a safety rule.

## What goes here

- Chats from the owner, and from testers who agreed to share them (ADR-0009 invariant 5).
- One file per chat, `<sport>/<YYYY-MM-DD>-<slug>.md`. A chat about several sports goes under `mixed/`.
- The athlete's messages verbatim, typos included. The app's replies as shown, or a note where they weren't captured.
- A short **Notes** section at the end: what went wrong or what is worth checking.

## What never goes here

- Names, emails, or places other than a gym's name.
- Screenshots or other images. Transcribe them instead.
- Anything a tester didn't agree to share.

## From transcript to eval case

1. Pick the messages that show a behaviour worth keeping, or a bug.
2. Add each one as a case in the template's smoke set, `evals/smoke/<template-id>.yaml`, with the expected fields and `metadata.field` set to this file's path.
3. Keep the message as the athlete wrote it. Don't tidy it up: the mess is the point.
