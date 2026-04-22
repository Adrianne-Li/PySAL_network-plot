# PySAL Ecosystem Network

An interactive dependency network visualization for the [PySAL](https://pysal.org/) ecosystem, rebuilt weekly by a GitHub Action.

**Live site:** [https://adrianne-li.github.io/PySAL_network-plot/](https://adrianne-li.github.io/PySAL_network-plot/)

---

## What it does

Every Monday, this repo automatically:

1. Spins up a clean Python environment and installs `pysal`
2. Walks the full dependency tree with `pipdeptree`
3. Fetches recent download counts from [PyPI Stats](https://pypistats.org/) and star / contributor counts from GitHub for every package in the tree
4. Renders an interactive force-directed network graph with [pyvis](https://pyvis.readthedocs.io/)
5. Commits the refreshed HTML and JSON back to `docs/`, which GitHub Pages serves as the live site

Click any node to see its dependencies, dependents, and popularity stats. Drag nodes to rearrange, scroll or use the zoom slider to navigate.

## Color legend

- 🔴 **Core PySAL** — the `pysal` meta-package
- 🟠 **PySAL modules** — submodules like `libpysal`, `esda`, `mapclassify`, etc.
- 🔵 **External dependencies** — everything else (`numpy`, `pandas`, `geopandas`, ...)

Node size scales with monthly PyPI downloads (log scale); edge thickness scales with the popularity of the dependency being pulled in.

## Repo structure

```
.
├── .github/workflows/
│   └── weekly-pysal-network-update.yml   # Runs every Monday at 13:00 UTC
├── docs/
│   ├── index.html                        # Landing page
│   ├── pysal_network.html                # Generated — the interactive graph
│   └── pysal_network_data.json           # Generated — raw graph data
├── scripts/
│   └── build_pysal_network.py            # The builder script
├── requirements.txt
└── README.md
```

Files under `docs/` with the word "generated" above are produced by the workflow — you don't need to edit them by hand.

## Running locally

```bash
pip install -r requirements.txt

# Optional but recommended — raises GitHub API rate limit from 60/hr to 5000/hr
export GITHUB_TOKEN=ghp_your_personal_access_token

python scripts/build_pysal_network.py
```

Outputs land in `docs/pysal_network.html` and `docs/pysal_network_data.json`. Open the HTML file in any browser to view.

The script builds its own temporary virtual environment internally to install `pysal` and `pipdeptree`, so it won't pollute your active Python environment with spatial-analysis libraries.

## Triggering a manual update

Go to the [Actions tab](../../actions), pick **"Weekly PySAL network update"**, and click **Run workflow**. Useful after merging changes to the builder script, or when you just want a fresh snapshot.

## Configuration

The workflow uses the repo's built-in `GITHUB_TOKEN` secret automatically — no setup required. For the scheduled cron to work, make sure:

- **Settings → Pages** is set to "Deploy from a branch", branch `main`, folder `/docs`
- **Settings → Actions → General → Workflow permissions** is set to "Read and write permissions"

## License

MIT
