# TurboBI — Combined BI Migration Suite

One web app, two converters, behind a single modern UI:

| Converter | Source | Backend | Output |
|---|---|---|---|
| **Tableau → Power BI** | `.twbx` / `.twb` upload | in-process (`tableau_to_pbi_agent`) | `.pbip` zip |
| **Qlik Sense → Power BI** | Qlik Cloud **App ID** | subprocess (`python -m qlik_to_pbi`) | `.pbip` zip |

The shell page (`/`) has a collapsible **burger sidebar** to switch between
the two converters. Each converter is rendered in an iframe of its own
embedded page, so both keep their full, independent feature set with zero
interference.

## Run

```bash
cd "TurboBI_Combined"
pip install flask requests          # plus each converter's own deps
python combined_app.py              # http://localhost:5000
python combined_app.py --port 8080  # custom port
```

Open <http://localhost:5000>. The Tableau converter loads by default; pick
**Qlik Sense** in the sidebar to switch.

## How it's wired

Both standalone apps owned the same root routes (`/`, `/convert`, `/stream`,
`/status`, `/download`), so they can't co-exist unprefixed. The combined app
mounts each as a **Flask Blueprint** under its own prefix and serves a shell at
the root:

```
/                       → shell (burger sidebar + iframe host)
/tableau/               → Tableau converter UI         (tableau_backend.py)
/tableau/convert, /tableau/stream/<id>, /tableau/status/<id>,
/tableau/download/<id>, /tableau/credentials-template, /tableau/save-credentials
/qlik/                  → Qlik converter UI            (qlik_backend.py)
/qlik/convert, /qlik/estimate, /qlik/tables, /qlik/db-connections,
/qlik/stream/<id>, /qlik/status/<id>, /qlik/log/<id>, /qlik/download/<id>
/healthz                → liveness probe
```

Nothing about either converter's behaviour changed — only the URL prefix. The
two backends are faithful ports of the original `app.py` files (the Qlik one
just has its package-relative imports rewritten to absolute, since it now lives
beside the `qlik_to_pbi` package rather than inside it).

## Layout

```
TurboBI_Combined/
├── combined_app.py        # entry point — registers both blueprints, serves the shell
├── tableau_backend.py     # Tableau blueprint (in-process conversion)
├── qlik_backend.py        # Qlik blueprint (subprocess conversion)
├── templates/
│   ├── index.html         # the unified shell (burger sidebar + iframe)
│   ├── tableau.html       # embedded Tableau UI (prefixed endpoints)
│   └── qlik.html          # embedded Qlik UI (prefixed endpoints)
├── tableau_to_pbi/        # converter core (copied, self-contained)
├── tableau_to_pbi_agent/  # LLM-assisted wrapper (copied)
└── qlik_to_pbi/           # Qlik converter package (copied)
```

## Notes

- **Theme** (light/dark) is shared: the shell and both child pages read the
  same `turbobi-theme` localStorage key, and the shell pushes the theme into
  the active iframe on switch and load.
- **Qlik prerequisites** are unchanged: a Qlik Cloud context
  (`~/.qlik/contexts.yml`) or the `qlik` CLI on PATH. The QVD fast-path needs
  `pyqvd` + `pyarrow`. Without these the Qlik converter behaves exactly as the
  standalone app did (the UI surfaces the same guidance).
- **Upload cap** for the Qlik QVD fast-path is governed by `QLIK_MAX_UPLOAD_MB`
  (default 8192).
