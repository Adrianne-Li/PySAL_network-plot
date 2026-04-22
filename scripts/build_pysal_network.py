#!/usr/bin/env python3
"""
Build a PySAL dependency network page with pyvis.

Outputs:
- docs/pysal_network.html
- docs/pysal_network_data.json

Pipeline:
1. Create temporary venv
2. Install pysal + pipdeptree
3. Extract dependency tree with pipdeptree --json-tree
4. Fetch package stats from PyPI / GitHub where possible
5. Build a pyvis network with a physics-based force-directed layout
6. Inject an interactive sidebar, zoom slider, and click-to-focus side panel
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from pyvis.network import Network


# ============================================================
# Configuration
# ============================================================

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs"))
REQUEST_TIMEOUT = 30
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

PYPISTATS_BASE = "https://pypistats.org/api/packages"
GITHUB_API_BASE = "https://api.github.com"

PYSAL_CORE = {"pysal"}
PYSAL_MODULES = {
    "access", "esda", "giddy", "inequality", "libpysal", "mapclassify",
    "mgwr", "momepy", "pointpats", "segregation", "splot", "spopt",
    "spreg", "spglm", "spaghetti", "spint", "tobler", "spvcm",
}

# Colors used in the previous HTML (matches the JS legend and panel emojis)
COLORS = {
    "core": "#d62728",      # red   -> Core PySAL
    "module": "#ff7f0e",    # orange -> PySAL modules
    "external": "#1f77b4",  # blue  -> External dependencies
}

# Legacy colors used in the previous pyvis output (red / orange / lightblue).
# We keep the richer COLORS for the edge/panel logic but set node `color`
# to the softer palette so it visually matches the prior page.
NODE_COLORS_LEGACY = {
    "core": "red",
    "module": "orange",
    "external": "lightblue",
}

GITHUB_REPO_MAP = {
    "pysal": ("pysal", "pysal"),
    "access": ("pysal", "access"),
    "esda": ("pysal", "esda"),
    "giddy": ("pysal", "giddy"),
    "inequality": ("pysal", "inequality"),
    "libpysal": ("pysal", "libpysal"),
    "mapclassify": ("pysal", "mapclassify"),
    "mgwr": ("pysal", "mgwr"),
    "momepy": ("pysal", "momepy"),
    "pointpats": ("pysal", "pointpats"),
    "segregation": ("pysal", "segregation"),
    "splot": ("pysal", "splot"),
    "spopt": ("pysal", "spopt"),
    "spreg": ("pysal", "spreg"),
    "spglm": ("pysal", "spglm"),
    "spaghetti": ("pysal", "spaghetti"),
    "spint": ("pysal", "spint"),
    "tobler": ("pysal", "tobler"),
    "geopandas": ("geopandas", "geopandas"),
    "numpy": ("numpy", "numpy"),
    "pandas": ("pandas-dev", "pandas"),
    "requests": ("psf", "requests"),
    "networkx": ("networkx", "networkx"),
    "scikit-learn": ("scikit-learn", "scikit-learn"),
    "scipy": ("scipy", "scipy"),
    "matplotlib": ("matplotlib", "matplotlib"),
    "seaborn": ("mwaskom", "seaborn"),
    "shapely": ("shapely", "shapely"),
    "pyproj": ("pyproj4", "pyproj"),
    "rasterio": ("rasterio", "rasterio"),
    "fiona": ("Toblerity", "Fiona"),
    "statsmodels": ("statsmodels", "statsmodels"),
}


# ============================================================
# Models
# ============================================================

@dataclass
class PackageInfo:
    name: str
    package_type: str
    downloads_last_month: Optional[int] = None
    contributors: Optional[int] = None
    stars: Optional[int] = None
    repo_url: Optional[str] = None
    pypi_url: Optional[str] = None


# ============================================================
# Utility helpers
# ============================================================

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "pysal-network-builder/2.0",
        "Accept": "application/json",
    })
    if GITHUB_TOKEN:
        session.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        session.headers["X-GitHub-Api-Version"] = "2022-11-28"
    return session


def request_json_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 5,
    base_sleep: float = 2.0,
    retry_on: Tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Any:
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if resp.status_code in retry_on:
                if attempt == max_retries:
                    resp.raise_for_status()

                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    sleep_s = float(retry_after)
                else:
                    sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.5)

                print(f"[retry] {url} -> {resp.status_code}, sleeping {sleep_s:.1f}s", file=sys.stderr)
                time.sleep(sleep_s)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.RequestException:
            if attempt == max_retries:
                raise
            sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(sleep_s)

    raise RuntimeError(f"Request failed: {url}")


def python_bin_from_venv(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=str(cwd) if cwd else None,
    )


def safe_num(x: Optional[int]) -> str:
    """Format a number; fall back to the string the JS panel expects."""
    return f"{x:,}" if isinstance(x, int) else "data not available for now"


def package_type(name: str) -> str:
    if name in PYSAL_CORE:
        return "core"
    if name in PYSAL_MODULES:
        return "module"
    return "external"


# ============================================================
# Dependency extraction
# ============================================================

def build_temp_env_and_extract_deps() -> List[Dict[str, Any]]:
    temp_dir = Path(tempfile.mkdtemp(prefix="pysal_net_"))
    venv_dir = temp_dir / "venv"

    print("[setup] creating temp venv", file=sys.stderr)
    venv.create(venv_dir, with_pip=True)
    py = python_bin_from_venv(venv_dir)

    print("[setup] installing pysal + pipdeptree", file=sys.stderr)
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "pysal", "pipdeptree"])

    print("[setup] extracting dependency tree", file=sys.stderr)
    result = run([str(py), "-m", "pipdeptree", "--packages", "pysal", "--json-tree"])

    if not result.stdout.strip():
        raise RuntimeError(f"pipdeptree returned no output.\nSTDERR:\n{result.stderr}")

    deps = json.loads(result.stdout)

    shutil.rmtree(temp_dir, ignore_errors=True)
    return deps


def build_graph_from_deps(deps: List[Dict[str, Any]]) -> Tuple[Set[str], List[Tuple[str, str]]]:
    node_names: Set[str] = set()
    edge_pairs: List[Tuple[str, str]] = []
    seen_edges: Set[Tuple[str, str]] = set()

    def add_edges(pkg: Dict[str, Any], parent: Optional[str] = None) -> None:
        name = pkg["key"]
        node_names.add(name)
        if parent is not None:
            pair = (parent, name)
            if pair not in seen_edges:
                seen_edges.add(pair)
                edge_pairs.append(pair)
        for dep in pkg.get("dependencies", []):
            add_edges(dep, name)

    for pkg in deps:
        add_edges(pkg)

    return node_names, edge_pairs


# ============================================================
# Metadata fetchers
# ============================================================

def fetch_pypi_last_month(session: requests.Session, package: str) -> Optional[int]:
    try:
        payload = request_json_with_retry(
            session,
            f"{PYPISTATS_BASE}/{package}/recent",
            headers={"Accept": "application/json"},
            max_retries=6,
            base_sleep=3.0,
        )
        time.sleep(1.5)
        return int(payload.get("data", {}).get("last_month", 0) or 0)
    except Exception:
        return None


def fetch_github_meta(session: requests.Session, package: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    mapping = GITHUB_REPO_MAP.get(package)
    if not mapping:
        return None, None, None

    owner, repo = mapping

    try:
        repo_meta = request_json_with_retry(
            session,
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
            max_retries=4,
            base_sleep=1.5,
            retry_on=(403, 429, 500, 502, 503, 504),
        )
        stars = int(repo_meta.get("stargazers_count", 0) or 0)

        contributors = 0
        page = 1
        while True:
            batch = request_json_with_retry(
                session,
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contributors",
                headers={"Accept": "application/vnd.github+json"},
                params={"per_page": 100, "anon": 1, "page": page},
                max_retries=4,
                base_sleep=1.5,
                retry_on=(403, 429, 500, 502, 503, 504),
            )
            if not isinstance(batch, list) or not batch:
                break
            contributors += len(batch)
            if len(batch) < 100:
                break
            page += 1
            time.sleep(0.2)

        return stars, contributors, f"https://github.com/{owner}/{repo}"

    except Exception:
        return None, None, None


def build_package_info(session: requests.Session, names: Set[str]) -> Dict[str, PackageInfo]:
    info: Dict[str, PackageInfo] = {}

    for name in sorted(names):
        ptype = package_type(name)
        downloads = fetch_pypi_last_month(session, name)
        stars, contributors, repo_url = fetch_github_meta(session, name)

        info[name] = PackageInfo(
            name=name,
            package_type=ptype,
            downloads_last_month=downloads,
            contributors=contributors,
            stars=stars,
            repo_url=repo_url,
            pypi_url=f"https://pypi.org/project/{name}/",
        )
        print(f"[meta] {name}", file=sys.stderr)

    return info


# ============================================================
# Network construction
# ============================================================

def compute_node_size(pkg: PackageInfo) -> float:
    """
    Size nodes by download volume on a log scale.

    The previous HTML used raw `value` in the 10-90 range and let vis.js
    scale them, so we reproduce that shape here.
    """
    if pkg.downloads_last_month is None or pkg.downloads_last_month <= 0:
        return 10.0
    # 10 + 6.5 * log10(downloads + 1) gives roughly 10..90 across PyPI scale
    return max(10.0, min(10.0 + 6.5 * math.log10(pkg.downloads_last_month + 1), 90.0))


def compute_edge_width(child_downloads: Optional[int], max_downloads: int) -> float:
    """
    Edge width scaled by the *child* (dependency) popularity, capped at 5.
    Matches the look of the previous file where popular deps had thick edges.
    """
    if child_downloads is None or child_downloads <= 0 or max_downloads <= 0:
        return 1.0
    frac = child_downloads / max_downloads
    return round(min(5.0, 1.0 + 4.0 * math.sqrt(frac)), 3)


def build_network_data(
    info: Dict[str, PackageInfo],
    edge_pairs: List[Tuple[str, str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    max_downloads = max((p.downloads_last_month or 0 for p in info.values()), default=0)

    nodes: List[Dict[str, Any]] = []
    for name, pkg in info.items():
        # IMPORTANT: the previous HTML's JS parses `node.title.split('|')`
        # expecting: name|downloads|contributors|stars
        # We must keep that exact format so the side panel keeps working.
        title = "|".join([
            name,
            safe_num(pkg.downloads_last_month),
            safe_num(pkg.contributors),
            safe_num(pkg.stars),
        ])

        nodes.append({
            "id": name,
            "label": name,
            "color": NODE_COLORS_LEGACY[pkg.package_type],
            "value": compute_node_size(pkg),
            "title": title,
            # Extra metadata preserved in the JSON sidecar only
            "_meta": {
                "package_type": pkg.package_type,
                "downloads_last_month": pkg.downloads_last_month,
                "contributors": pkg.contributors,
                "stars": pkg.stars,
                "repo_url": pkg.repo_url,
                "pypi_url": pkg.pypi_url,
            },
        })

    edges: List[Dict[str, Any]] = []
    for src, dst in edge_pairs:
        src_type = info[src].package_type if src in info else "external"
        dst_downloads = info[dst].downloads_last_month if dst in info else None

        edges.append({
            "from": src,
            "to": dst,
            "color": COLORS.get(src_type, "#999999"),
            "width": compute_edge_width(dst_downloads, max_downloads),
        })

    return nodes, edges


def build_pyvis_network(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Network:
    """
    Build the pyvis network using the physics-based ForceAtlas2 layout
    that matches the look and feel of the previous HTML.
    """
    net = Network(
        height="100vh",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="black",
        notebook=False,
        cdn_resources="remote",
    )

    # These options match the previous HTML's behavior: force-directed,
    # stabilized layout, smooth edges, and physics-driven node spreading.
    net.set_options("""
    {
      "configure": { "enabled": false },
      "edges": {
        "color": { "inherit": true },
        "smooth": { "enabled": true, "type": "dynamic" }
      },
      "interaction": {
        "dragNodes": true,
        "hideEdgesOnDrag": false,
        "hideNodesOnDrag": false,
        "hover": true
      },
      "physics": {
        "enabled": true,
        "forceAtlas2Based": {
          "avoidOverlap": 0,
          "centralGravity": 0.01,
          "damping": 0.4,
          "gravitationalConstant": -50,
          "springConstant": 0.08,
          "springLength": 100
        },
        "solver": "forceAtlas2Based",
        "stabilization": {
          "enabled": true,
          "fit": true,
          "iterations": 1000,
          "onlyDynamicEdges": false,
          "updateInterval": 50
        }
      }
    }
    """)

    for node in nodes:
        net.add_node(
            node["id"],
            label=node["label"],
            color=node["color"],
            value=node["value"],
            title=node["title"],
            shape="dot",
            font={"color": "black"},
        )

    for edge in edges:
        net.add_edge(
            edge["from"],
            edge["to"],
            color=edge["color"],
            width=edge["width"],
            arrows="to",
        )

    return net


# ============================================================
# HTML post-processing: inject the sidebar, zoom slider, side panel
# ============================================================

SIDEBAR_AND_PANEL_HTML = r"""
<!-- Title and Instructions on the left side -->
<div style="position: fixed; left: 20px; top: 20px; width: 350px; z-index: 100;
background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;
border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.2); padding: 20px;">
    <h1 style="margin: 0 0 10px 0; font-size: 1.8em; font-weight: bold;
               text-shadow: 2px 2px 4px rgba(0,0,0,0.3);">
        🌐 PySAL Ecosystem Network
    </h1>
    <p style="margin: 0 0 15px 0; font-size: 1em; opacity: 0.9;">
        Interactive dependency visualization
    </p>
    <div style="background: rgba(255,255,255,0.1); padding: 15px; border-radius: 10px; margin: 10px 0;">
        <h3 style="margin: 0 0 10px 0; font-size: 1.1em;">📋 How to Use:</h3>
        <div style="font-size: 0.9em; line-height: 1.4;">
            <div style="margin: 8px 0; display: flex; align-items: center;">
                <span style="font-size: 1.2em; margin-right: 8px;">🖱️</span>
                <span><strong>Click</strong> node → view connections</span>
            </div>
            <div style="margin: 8px 0; display: flex; align-items: center;">
                <span style="font-size: 1.2em; margin-right: 8px;">👆</span>
                <span><strong>Hover</strong> → basic info</span>
            </div>
            <div style="margin: 8px 0; display: flex; align-items: center;">
                <span style="font-size: 1.2em; margin-right: 8px;">✋</span>
                <span><strong>Drag</strong> → pan around</span>
            </div>
        </div>
        <!-- Interactive Zoom Slider -->
        <div style="margin-top: 15px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.3);">
            <div style="margin-bottom: 8px; font-size: 0.9em; font-weight: bold;">🔍 Zoom Control:</div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 0.8em;">−</span>
                <input type="range" id="zoom-slider" min="0.1" max="3" step="0.1" value="1"
                       style="flex: 1; height: 6px; background: rgba(255,255,255,0.3);
                              border-radius: 3px; outline: none; cursor: pointer;">
                <span style="font-size: 0.8em;">+</span>
            </div>
            <div style="text-align: center; font-size: 0.8em; margin-top: 4px; opacity: 0.8;">
                Zoom: <span id="zoom-level">100%</span>
            </div>
        </div>
    </div>
    <div style="background: rgba(255,255,255,0.1); padding: 12px; border-radius: 10px; font-size: 0.9em;">
        <div style="margin-bottom: 8px;"><strong>🎨 Color Legend:</strong></div>
        <div style="margin: 5px 0; display: flex; align-items: center;">
            <span style="display: inline-block; width: 12px; height: 12px; background: #d62728;
                         border-radius: 50%; margin-right: 8px;"></span>
            <span>Core PySAL</span>
        </div>
        <div style="margin: 5px 0; display: flex; align-items: center;">
            <span style="display: inline-block; width: 12px; height: 12px; background: #ff7f0e;
                         border-radius: 50%; margin-right: 8px;"></span>
            <span>PySAL Modules</span>
        </div>
        <div style="margin: 5px 0; display: flex; align-items: center;">
            <span style="display: inline-block; width: 12px; height: 12px; background: #1f77b4;
                         border-radius: 50%; margin-right: 8px;"></span>
            <span>External Dependencies</span>
        </div>
    </div>
</div>

<!-- Side panel for node info -->
<div id="side-panel" style="display:none; position:fixed; right:20px; top:20px; bottom:20px;
width:350px; background:white; padding:20px; border-radius:15px; overflow-y:auto;
box-shadow:0 8px 32px rgba(0,0,0,0.3); z-index:1000;">
    <button id="close-panel" style="float:right; background:#e74c3c; color:white; border:none;
            border-radius:50%; width:35px; height:35px; cursor:pointer; font-size:18px;
            margin-left:10px;">&times;</button>
    <div id="panel-body"></div>
</div>

<script type="text/javascript">
// List of PySAL module ids (used to tag nodes in the side panel legend)
var PYSAL_MODULE_IDS = ["access","esda","giddy","inequality","libpysal","mapclassify","mgwr",
    "momepy","pointpats","segregation","splot","spopt","spreg","spglm","spvcm","spaghetti",
    "spint","tobler"];

function nodeTypeLabel(nodeId) {
    if (nodeId === "pysal") return "Core PySAL Package";
    if (PYSAL_MODULE_IDS.indexOf(nodeId) !== -1) return "PySAL Module";
    return "External Dependency";
}
function nodeTypeEmoji(nodeId) {
    if (nodeId === "pysal") return "🔴";
    if (PYSAL_MODULE_IDS.indexOf(nodeId) !== -1) return "🟠";
    return "🔵";
}

// Wait for the network to be ready
setTimeout(function() {
    if (typeof window.network !== 'undefined') {
        var nodes = window.network.body.data.nodes;
        var edges = window.network.body.data.edges;
        // Store original values including label
        nodes.forEach(function(data, id) {
            nodes.update({
                id: id,
                originalValue: data.value || 10,
                originalColor: data.color,
                originalFont: data.font || {size: 14, color: 'black', face: 'Arial',
                    strokeWidth: 1, strokeColor: 'white', align: 'center', vadjust: 0},
                originalLabel: data.label || id
            });
        });
        edges.forEach(function(data, id) {
            edges.update({id: id, originalColor: data.color});
        });
        window.network.fit();
        window.network.once('stabilized', function() {
            var scale = window.network.getScale();
            document.getElementById('zoom-slider').value = scale;
            document.getElementById('zoom-level').textContent = Math.round(scale * 100) + '%';
            window.network.redraw();
        });
        setupPanel();
        setupZoomSlider();
    }
}, 2000);

function setupZoomSlider() {
    var slider = document.getElementById('zoom-slider');
    var zoomLevel = document.getElementById('zoom-level');
    slider.addEventListener('input', function() {
        var scale = parseFloat(this.value);
        window.network.moveTo({ scale: scale });
        zoomLevel.textContent = Math.round(scale * 100) + '%';
    });
    window.network.on('zoom', function(params) {
        var currentScale = params.scale;
        slider.value = currentScale;
        zoomLevel.textContent = Math.round(currentScale * 100) + '%';
        window.network.redraw();
    });
}

function setupPanel() {
    var panel = document.getElementById('side-panel');
    var closeBtn = document.getElementById('close-panel');
    closeBtn.onclick = function() {
        panel.style.display = 'none';
        resetNetwork();
    };
    window.network.on("click", function(params) {
        if (params.nodes.length === 0) {
            panel.style.display = 'none';
            resetNetwork();
            return;
        }
        var nodeId = params.nodes[0];
        showNodePanel(nodeId);
    });
}

function resetNetwork() {
    var nodes = window.network.body.data.nodes;
    var edges = window.network.body.data.edges;
    nodes.update(nodes.get().map(function(node) {
        return {
            id: node.id,
            color: node.originalColor,
            value: node.originalValue,
            hidden: false,
            font: node.originalFont,
            label: node.originalLabel
        };
    }));
    edges.update(edges.get().map(function(edge) {
        return { id: edge.id, color: edge.originalColor, hidden: false };
    }));
    window.network.setOptions({physics: true});
    setTimeout(function() {
        window.network.stabilize();
        window.network.fit();
        window.network.redraw();
        setTimeout(function() {
            window.network.redraw();
            var currentScale = window.network.getScale();
            window.network.moveTo({scale: currentScale * 1.01});
            setTimeout(function() {
                window.network.moveTo({scale: currentScale});
                window.network.redraw();
            }, 50);
        }, 500);
    }, 500);
    window.network.once('stabilized', function() {
        window.network.redraw();
    });
}

function showNodePanel(nodeId) {
    var nodes = window.network.body.data.nodes;
    var edges = window.network.body.data.edges;
    var allNodes = nodes.get();
    var allEdges = edges.get();

    var dependencies = [];
    var dependents = [];
    allEdges.forEach(function(edge) {
        if (edge.from === nodeId) dependencies.push(edge.to);
        if (edge.to === nodeId)   dependents.push(edge.from);
    });
    var uniqueDeps = Array.from(new Set(dependencies));
    var uniqueDepents = Array.from(new Set(dependents));
    var connectedNodes = Array.from(new Set(uniqueDeps.concat(uniqueDepents)));
    var visibleNodesSet = new Set([nodeId].concat(connectedNodes));

    nodes.update(allNodes.map(function(node) {
        var isVisible = visibleNodesSet.has(node.id);
        var enlargementFactor = isVisible ? 3 : 1;
        var fontSize = isVisible ? 20 : node.originalFont.size;
        var strokeWidth = isVisible ? 1.5 : node.originalFont.strokeWidth;
        var newFont = Object.assign({}, node.originalFont,
            {size: fontSize, strokeWidth: strokeWidth});
        return {
            id: node.id,
            hidden: !isVisible,
            value: node.originalValue * enlargementFactor,
            color: node.originalColor,
            font: newFont,
            label: node.originalLabel
        };
    }));

    edges.update(allEdges.map(function(edge) {
        var isVisible = visibleNodesSet.has(edge.from) && visibleNodesSet.has(edge.to);
        return { id: edge.id, hidden: !isVisible, color: edge.originalColor };
    }));

    window.network.setOptions({physics: false});
    setTimeout(function() {
        window.network.stabilize();
        window.network.fit();
        window.network.redraw();
        setTimeout(function() {
            window.network.redraw();
            var currentScale = window.network.getScale();
            window.network.moveTo({scale: currentScale * 1.01});
            setTimeout(function() {
                window.network.moveTo({scale: currentScale});
                window.network.redraw();
            }, 50);
        }, 500);
    }, 500);

    var selectedNode = allNodes.find(function(n) { return n.id === nodeId; });
    var titleParts = (selectedNode.title || "").split('|');
    var downloads    = titleParts[1] || "data not available for now";
    var contributors = titleParts[2] || "data not available for now";
    var stars        = titleParts[3] || "data not available for now";

    var content = '';
    content += '<h1 style="text-align:center; color:#2c3e50; margin-bottom:20px;">PySAL Network Analysis</h1>';
    content += '<h2 style="text-align:center; color:#3498db; margin-bottom:5px;">' + nodeId + '</h2>';
    content += '<p style="text-align:center; color:#7f8c8d; font-style:italic; margin-bottom:20px;">'
             + nodeTypeLabel(nodeId) + '</p>';
    content += '<div style="background:#f8f9fa; padding:15px; border-radius:8px; margin-bottom:20px;">';
    content += '<h3 style="margin-top:0;">📊 Package Statistics</h3>';
    content += '<p>📥 Downloads: ' + downloads + '</p>';
    content += '<p>👥 Contributors: ' + contributors + '</p>';
    content += '<p>⭐ GitHub Stars: ' + stars + '</p>';
    content += '</div>';

    content += '<h3 style="color:#2c3e50; border-bottom:2px solid #3498db; padding-bottom:5px;">📦 Dependencies ('
             + uniqueDeps.length + ')</h3>';
    if (uniqueDeps.length > 0) {
        content += '<div style="margin-bottom:20px;">';
        uniqueDeps.forEach(function(depId) {
            var depNode = allNodes.find(function(n) { return n.id === depId; });
            var depParts = (depNode && depNode.title ? depNode.title : depId + '|n/a|n/a|n/a').split('|');
            content += '<div style="border:1px solid #ddd; padding:10px; margin:5px 0; border-radius:5px;">';
            content += '<strong>' + nodeTypeEmoji(depId) + ' ' + depId + '</strong><br>';
            content += '<small>Downloads: ' + depParts[1] + ' | Contributors: ' + depParts[2]
                     + ' | Stars: ' + depParts[3] + '</small>';
            content += '</div>';
        });
        content += '</div>';
    } else {
        content += '<p style="color:#7f8c8d; font-style:italic;">No dependencies</p>';
    }

    content += '<h3 style="color:#2c3e50; border-bottom:2px solid #27ae60; padding-bottom:5px;">📋 Dependents ('
             + uniqueDepents.length + ')</h3>';
    if (uniqueDepents.length > 0) {
        content += '<div style="margin-bottom:20px;">';
        uniqueDepents.forEach(function(depId) {
            var depNode = allNodes.find(function(n) { return n.id === depId; });
            var depParts = (depNode && depNode.title ? depNode.title : depId + '|n/a|n/a|n/a').split('|');
            content += '<div style="border:1px solid #ddd; padding:10px; margin:5px 0; border-radius:5px;">';
            content += '<strong>' + nodeTypeEmoji(depId) + ' ' + depId + '</strong><br>';
            content += '<small>Downloads: ' + depParts[1] + ' | Contributors: ' + depParts[2]
                     + ' | Stars: ' + depParts[3] + '</small>';
            content += '</div>';
        });
        content += '</div>';
    } else {
        content += '<p style="color:#7f8c8d; font-style:italic;">No dependents</p>';
    }

    content += '<div style="background:#2c3e50; color:white; padding:10px; border-radius:8px; text-align:center;">';
    content += '<strong>Legend:</strong> 🔴 Core PySAL | 🟠 PySAL Modules | 🔵 External Dependencies';
    content += '</div>';

    document.getElementById('panel-body').innerHTML = content;
    document.getElementById('side-panel').style.display = 'block';
}
</script>
"""


def inject_sidebar_and_panel(html: str) -> str:
    """
    pyvis defines the network as a local `var network` inside drawGraph(),
    but our JS needs `window.network`. We patch both:
      - expose the network globally
      - append the sidebar + zoom slider + side panel markup and JS
    """
    # Expose the network on window so our panel code can reach it.
    html = html.replace(
        "network = new vis.Network(container, data, options);",
        "network = new vis.Network(container, data, options); window.network = network;",
        1,
    )

    # Make sure the network canvas fills the viewport (the sidebar overlays it).
    html = html.replace(
        "height: 1200px;",
        "height: 100vh;",
        1,
    )

    # Inject our sidebar + panel right before </body>.
    if "</body>" in html:
        html = html.replace("</body>", SIDEBAR_AND_PANEL_HTML + "\n</body>", 1)
    else:
        html += SIDEBAR_AND_PANEL_HTML

    return html


# ============================================================
# Writers
# ============================================================

def write_json(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pysal_network_data.json"

    # Flatten _meta back out for the JSON sidecar
    json_nodes = []
    for n in nodes:
        meta = n.get("_meta", {})
        json_nodes.append({
            "id": n["id"],
            "label": n["label"],
            "color": n["color"],
            "value": n["value"],
            "title": n["title"],
            **meta,
        })

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "nodes": json_nodes,
        "edges": edges,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_html(net: Network, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pysal_network.html"
    net.write_html(str(path), open_browser=False, notebook=False)

    html = path.read_text(encoding="utf-8")
    html = inject_sidebar_and_panel(html)
    path.write_text(html, encoding="utf-8")
    return path


# ============================================================
# Main
# ============================================================

def main() -> int:
    session = build_session()

    deps = build_temp_env_and_extract_deps()
    node_names, edge_pairs = build_graph_from_deps(deps)
    info = build_package_info(session, node_names)
    nodes, edges = build_network_data(info, edge_pairs)

    net = build_pyvis_network(nodes, edges)

    json_path = write_json(nodes, edges, OUTPUT_DIR)
    html_path = write_html(net, OUTPUT_DIR)

    print(f"Wrote JSON: {json_path}")
    print(f"Wrote HTML: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
