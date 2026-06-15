# Network Command Translator

Pick a task, get the equivalent CLI command for every vendor side by side — Aruba AOS-CX, Aruba AOS-S, Juniper Junos, Cisco IOS, Ruckus FastIron, plus Juniper Mist dashboard notes. Static site: plain HTML/CSS/JS, no build step, no backend.

## Run locally

`fetch()` needs HTTP, so don't open `index.html` from the filesystem:

```sh
python3 -m http.server
# → http://localhost:8000
```

## Live variables

The sticky bar substitutes your values into every command on screen. In `data/commands.json`, placeholders are written as `{{interface}}`, `{{vlan}}`, `{{ip}}`, `{{hostname}}`. While a variable is empty, commands show (and copy) an amber example value instead — per-vendor for interfaces (`1/1/1` on Aruba, `ge-0/0/0` on Junos, `gi0/1` on IOS) so you know what format to swap in. The copy button always copies exactly what's shown.

One caveat: the interface field is shared across vendors, so type the format of the vendor you're about to copy (`ge-0/0/24` for Junos, `gi0/24` for IOS, …).

## Data format

`data/commands.json` is an array of entries:

```json
{
  "id": "show-interface-errors",
  "task": "Show interface errors",
  "category": "Interfaces",
  "tags": ["errors", "counters", "troubleshooting"],
  "commands": {
    "aos-cx": "show interface {{interface}} extended",
    "junos": "show interfaces {{interface}} extensive"
  },
  "mist_note": "Mist dashboard: Switches > [switch] > port details",
  "notes": "One-sentence cross-vendor gotcha.",
  "verify": ["junos"]
}
```

- Omit a vendor key when there's no equivalent — the UI shows *no direct equivalent*.
- `mist_note` is a dashboard path, not a CLI command (Mist is cloud-managed).
- `verify` lists vendor ids whose syntax hasn't been confirmed on real gear yet (shown with an amber *unverified* dot). `"verify": true` flags every vendor in the entry.
- Conventions: show commands are one line from operational/enable mode; config tasks are newline-separated and assume global config mode (Junos uses `set …` lines, commit omitted).

## Validate before committing data changes

```sh
python3 tools/validate.py
```

Checks duplicate ids, unknown vendor keys, missing required fields, bad `verify` values, and unknown `{{placeholders}}`. Exits non-zero on errors.

## Deploy

Push to GitHub, enable Pages (deploy from branch, root). No build step needed. On `*.github.io` the "request a command" link auto-derives the repo from the URL; for other hosts, update the `GITHUB_REPO` fallback at the top of `app.js`.
