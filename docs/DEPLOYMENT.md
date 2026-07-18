# Public frontend with a local companion

## Recommended architecture

```text
Vercel HTTPS frontend
        |
        | browser fetch + local-network permission + pairing token
        v
http://127.0.0.1:8765 on each friend's computer
        |
        +-- selected local workspace
        +-- LocalAgent tools and permissions
        +-- that computer's Ollama and local model
```

Vercel hosts only the static interface. It does not proxy prompts, files, shell
commands, or model traffic, because a Vercel server cannot address the
`127.0.0.1` belonging to an arbitrary visitor. Each friend installs and starts
the local companion and Ollama on their own computer.

After cloning the repository, each friend runs `scripts\setup.ps1` once on
their own computer. Model files and inference remain on that computer; they
are not downloaded to or executed on the original developer's machine.

Chrome treats a request from a public site to loopback as Local Network Access
and may display a permission prompt. The companion therefore supports an exact
HTTPS origin allowlist, CORS preflight, and a per-launch pairing token. See the
[Chrome Local Network Access guidance](https://developer.chrome.com/blog/local-network-access)
and [Vercel static build output documentation](https://vercel.com/docs/builds).

## Local companion startup

Each user starts the companion with a default workspace:

```powershell
.\scripts\start-ui.ps1 `
  -Workspace 'D:\projects\their-project' `
  -AllowedOrigin 'https://your-project.vercel.app' `
  -NoBrowser
```

If `-Workspace` is omitted, the script opens a native folder picker. The
terminal prints a one-time pairing token. The public frontend asks for that
token on its first connection and stores it only in that browser. Clicking
“New Task” then opens the local folder picker again; each task receives its own
workspace binding and session.
The companion starts listening before model warmup completes, so a cold model
is reported as loading instead of making the frontend appear disconnected.

## Vercel frontend

The repository includes `vercel.json`, which exposes the files under `web/` as
a static site. Import the GitHub repository into Vercel without adding an
Ollama or backend environment variable. The browser connects directly to the
visitor's loopback companion.

## Security boundaries

- Keep the companion bound to `127.0.0.1`; never bind it to `0.0.0.0`.
- Allow only the exact production Vercel origin, not `*`.
- Every user runs their own model and downloads their own model files.
- The companion owns a default workspace and a bounded task-to-workspace map;
  each task is isolated to the directory selected when it was created.
- Do not enable Auto mode for untrusted shared machines without an additional
  command approval policy and OS sandbox.
- Vercel never receives local workspace contents in this architecture.
