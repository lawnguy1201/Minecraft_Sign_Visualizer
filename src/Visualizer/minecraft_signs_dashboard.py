# Made by lawnguy to see sign visualizations and make video animations of sign placements
# used claude to help speed up development
# 5/19/26

import os, sys, traceback, threading, math, duckdb, shutil as _shutil_mod
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.io as pio
import tempfile, shutil, subprocess, imageio_ffmpeg

def _find_browser():
    if sys.platform == "win32":
        for path in [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if os.path.exists(path): return path
    elif sys.platform == "darwin":
        for path in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]:
            if os.path.exists(path): return path
    for name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "microsoft-edge"]:
        found = _shutil_mod.which(name)
        if found: return found
    return None

def _frame_html(fig, w, h):
    bg  = fig.layout.paper_bgcolor or "#ffffff"
    div = pio.to_html(
        fig, full_html=False, include_plotlyjs=True,
        config={"displayModeBar": False, "scrollZoom": False},
        div_id="plot",
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box}'
        f'html,body{{width:{w}px;height:{h}px;overflow:hidden;background:{bg}}}'
        f'#plot{{width:{w}px!important;height:{h}px!important}}'
        '.js-plotly-plot,.plotly,.plot-container{width:100%!important;height:100%!important}'
        '</style></head><body>' + div + '</body></html>'
    )

import dash_bootstrap_components as dbc
from dash import Dash, dcc, html, Input, Output, State, ctx, no_update
from dash_bootstrap_templates import ThemeSwitchAIO, load_figure_template

LIGHT_THEME = dbc.themes.FLATLY
DARK_THEME  = dbc.themes.SLATE
load_figure_template(["flatly", "slate"])

_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIM_FILES = {
    "end":    os.path.join(_parent, "data", "signs_pretty_end.csv"      ).replace("\\", "/"),
    "nether": os.path.join(_parent, "data", "signs_pretty_nether.csv"   ).replace("\\", "/"),
    "over":   os.path.join(_parent, "data", "signs_pretty_overworld.csv").replace("\\", "/"),
}
DIM_LABELS = {"end": "The End", "nether": "Nether", "over": "Overworld"}
for k, p in DIM_FILES.items():
    print(f"{'OK' if os.path.exists(p) else 'MISSING'}: {k} -> {p}")

conn     = duckdb.connect()
_db_lock = threading.Lock()
_FETCH_CACHE:   dict = {}
_CLUSTER_CACHE: dict = {}
_DATE_CACHE:    dict = {}

def _exec(q, params=None):
    with _db_lock:
        result = conn.execute(q, params) if params else conn.execute(q)
        df = result.df()
    return df if df is not None else pd.DataFrame()

def _exec_one(q, params=None):
    with _db_lock:
        result = conn.execute(q, params) if params else conn.execute(q)
        row = result.fetchone()
    return row[0] if row else None


VALID_GRID_SIZES = {128, 256, 512, 1024, 2048}

def _safe_grid(gs):
    v = int(gs)
    if v not in VALID_GRID_SIZES:
        raise ValueError(f"Invalid grid_size {v!r} — must be one of {VALID_GRID_SIZES}")
    return v

def _safe_coord(v, name):
    f = float(v)
    if not math.isfinite(f) or not (-30_000_000 <= f <= 30_000_000):
        raise ValueError(f"Coordinate {name}={v!r} is out of valid Minecraft bounds")
    return f

def _extract_dates(df):
    raw = df["plain_text"].str.extract(
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", expand=False).str.strip()
    return (pd.to_datetime(raw, format="%d %b %Y", errors="coerce")
              .fillna(pd.to_datetime(raw, format="%d %B %Y", errors="coerce")))

def _add_dates(df):
    df = df.copy()
    df["sign_date"] = _extract_dates(df)
    df["date_numeric"] = np.nan
    dated = df["sign_date"].notna()
    if dated.any():
        min_d = df.loc[dated, "sign_date"].min()
        df.loc[dated, "date_numeric"] = (df.loc[dated, "sign_date"] - min_d).dt.days
    return df.sort_values("sign_date").reset_index(drop=True)

def _anim_clip(df, limit=1_000_000):
    if df.empty: return df.reset_index(drop=True)
    return df[(df["x"].abs() <= limit) & (df["z"].abs() <= limit)].reset_index(drop=True)

def _clip(df, lo=0.001, hi=0.999):
    if df.empty or len(df) < 20:
        return df.reset_index(drop=True)
    out = df.copy()
    for col in ("x", "z"):
        out = out[(out[col] >= out[col].quantile(lo)) & (out[col] <= out[col].quantile(hi))]
    return out.reset_index(drop=True)

def _where(filt, text):
    clauses, params = [], []
    if filt == "cody":
        clauses.append("plain_text ILIKE '%codysmile11%'")
    if text:
        clauses.append("plain_text ILIKE ?")
        params.append(f"%{text}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params

def fetch_signs(dim, filt, text, max_pts):
    text = (text or "").strip()
    key = (dim, filt, text.lower(), int(max_pts))
    if key in _FETCH_CACHE:
        return _FETCH_CACHE[key]
    file = DIM_FILES[dim]
    where, params = _where(filt, text)
    total = _exec_one(f"SELECT COUNT(*) FROM read_csv_auto('{file}') {where}", params or None) or 0
    print(f"[fetch] {dim}/{filt} total={total:,}")
    if total == 0:
        empty = pd.DataFrame(columns=["x","y","z","plain_text","sign_date","date_numeric"])
        _FETCH_CACHE[key] = (empty, 0, 0); return _FETCH_CACHE[key]
    if max_pts == 0 or total <= max_pts:
        q, fetched = f"SELECT x,y,z,plain_text FROM read_csv_auto('{file}') {where}", total
    else:
        q = (f"SELECT x,y,z,plain_text FROM "
             f"(SELECT x,y,z,plain_text FROM read_csv_auto('{file}') {where}) "
             f"USING SAMPLE {max_pts} ROWS")
        fetched = max_pts
    df = _add_dates(_exec(q, params or None))
    print(f"[fetch] got {len(df):,}  dated={df['sign_date'].notna().sum():,}")
    _FETCH_CACHE[key] = (df, total, fetched)
    return _FETCH_CACHE[key]

def fetch_clusters(dim, filt, text, grid_size):
    text = (text or "").strip()
    key = ("cluster", dim, filt, text.lower(), grid_size)
    if key in _CLUSTER_CACHE:
        return _CLUSTER_CACHE[key]
    file = DIM_FILES[dim]
    where, params = _where(filt, text)
    gs = _safe_grid(grid_size)
    q = f"""SELECT CAST(floor(x/{gs})*{gs}+{gs//2} AS INTEGER) AS cx,
                   CAST(AVG(y) AS INTEGER) AS cy,
                   CAST(floor(z/{gs})*{gs}+{gs//2} AS INTEGER) AS cz,
                   COUNT(*) AS n
            FROM read_csv_auto('{file}') {where}
            GROUP BY floor(x/{gs}), floor(z/{gs})"""
    df = _exec(q, params or None)
    if len(df) >= 20:
        for col in ("cx","cz"):
            df = df[(df[col] >= df[col].quantile(0.005)) & (df[col] <= df[col].quantile(0.995))]
        df = df.reset_index(drop=True)
    print(f"[clusters] {dim}/{filt} grid={grid_size} → {len(df):,} cells")
    _CLUSTER_CACHE[key] = df
    return df

def fetch_cluster_area(dim, filt, text, cx, cz, gs):
    text = (text or "").strip()
    file = DIM_FILES[dim]
    where, params = _where(filt, text)
    cx = _safe_coord(cx, "cx")
    cz = _safe_coord(cz, "cz")
    gs = _safe_grid(gs)
    half = gs * 1.5
    x_min, x_max = cx - half, cx + half
    z_min, z_max = cz - half, cz + half
    spatial = f"x BETWEEN {x_min} AND {x_max} AND z BETWEEN {z_min} AND {z_max}"
    full_where = f"{where} AND {spatial}" if where else f"WHERE {spatial}"
    q  = f"SELECT x,y,z,plain_text FROM read_csv_auto('{file}') {full_where}"
    df = _add_dates(_exec(q, params or None))
    print(f"[drill] cx={cx} cz={cz} gs={gs} → {len(df):,} signs")
    return df

def anim_steps(dated_df, gran, sd, ed):
    if dated_df.empty or not sd or not ed:
        return []
    sub = dated_df[(dated_df["sign_date"].dt.date >= sd) & (dated_df["sign_date"].dt.date <= ed)]
    if sub.empty:
        return []
    if gran == "day":   return sorted(sub["sign_date"].dt.date.unique())
    if gran == "week":  return sorted(sub["sign_date"].dt.to_period("W").dt.start_time.dt.date.unique())
    return sorted(sub["sign_date"].dt.to_period("M").dt.start_time.dt.date.unique())

def fetch_date_range(dim, filt, text):
    text = (text or "").strip()
    key  = (dim, filt, text.lower())
    if key in _DATE_CACHE:
        return _DATE_CACHE[key]
    file = DIM_FILES[dim]
    where, params = _where(filt, text)
    df = _exec(f"SELECT plain_text FROM read_csv_auto('{file}') {where}", params or None)
    if df.empty:
        _DATE_CACHE[key] = (None, None); return (None, None)
    dates = _extract_dates(df).dropna()
    if dates.empty:
        _DATE_CACHE[key] = (None, None); return (None, None)
    result = (str(dates.min().date()), str(dates.max().date()))
    _DATE_CACHE[key] = result
    return result

VIEWS = {
    "Default 3D":   dict(eye=dict(x=1.6,  y=1.6,  z=1.0),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=0,z=1)),
    "Top Down":     dict(eye=dict(x=0,    y=0,    z=3.0),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=1,z=0)),
    "Front":        dict(eye=dict(x=0,    y=3.0,  z=0  ),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=0,z=1)),
    "Side":         dict(eye=dict(x=3.0,  y=0,    z=0  ),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=0,z=1)),
    "Isometric NE": dict(eye=dict(x=1.5,  y=1.5,  z=1.5),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=0,z=1)),
    "Isometric SW": dict(eye=dict(x=-1.5, y=-1.5, z=1.5),  center=dict(x=0,y=0,z=0), up=dict(x=0,y=0,z=1)),
}
COLOR_OPTIONS = {"Date placed":"date_numeric", "Y level":"y", "X coord":"x", "Z coord":"z"}
COLORSCALES   = ["Viridis","Turbo","Plasma","Inferno","Magma","YlOrRd","Blues"]
MAX_PTS_OPTS = [{"label":"500 (fast)","value":500},{"label":"2 500","value":2_500},
                 {"label":"5 000","value":5_000},{"label":"10 000","value":10_000},
                 {"label":"25 000 (slow)","value":25_000},
                 {"label":"All signs (no limit — can be very slow)","value":0}]
GRID_OPTS     = [{"label":"128 (fine)","value":128},{"label":"256","value":256},
                 {"label":"512","value":512},{"label":"1024","value":1024},
                 {"label":"2048 (map)","value":2048}]
_SWITCH_ID    = ThemeSwitchAIO.ids.switch("theme")

def build_figure(dated_df, undated_df, full_df, color_col, colorscale,
                 marker_size, camera, dark_mode, show_undated,
                 view_name="Default 3D", cluster_df=None,
                 overlay_title=None, overlay_date=None):
    dark     = bool(dark_mode)
    template = "slate"   if dark else "flatly"
    bg       = "#111827" if dark else "#f8f9fa"
    scene_bg = "#1a2035" if dark else "#eef2ff"
    grid_col = "#2d3a5e" if dark else "#c7d2fe"
    text_col = "#e2e8f0" if dark else "#1e293b"

    def _mk(df, size, opacity=0.88, symbol="circle"):
        c = df[color_col].values.astype(float)
        ref = full_df[color_col].dropna().values.astype(float) if (full_df is not None and not full_df.empty and color_col in full_df.columns) else c
        cmin = float(np.nanmin(ref)) if len(ref) else 0.0
        cmax = float(np.nanmax(ref)) if len(ref) else 1.0
        if cmin == cmax: cmax = cmin + 1.0
        return dict(size=size, opacity=opacity, symbol=symbol, line=dict(width=0),
                    color=c.tolist(), colorscale=colorscale, cmin=cmin, cmax=cmax,
                    colorbar=dict(thickness=12, len=0.5, title=color_col))

    def _ax(label, rng=None):
        d = dict(title=dict(text=label, font=dict(color=text_col)),
                 backgroundcolor=scene_bg, gridcolor=grid_col,
                 showbackground=True, tickfont=dict(color=text_col))
        if rng: d["range"] = rng
        return d

    def _sym(vals):
        if vals is None or len(vals) == 0: return None
        a = max(abs(float(np.nanmin(vals))), abs(float(np.nanmax(vals))), 1.0)
        return [-a, a]

    def _rng(vals, cap=None):

        if vals is None or len(vals) == 0: return None
        a = np.asarray(vals, dtype=float)
        a = a[np.isfinite(a)]
        if len(a) == 0: return None
        if cap is not None:
            a = a[np.abs(a) <= cap]
            if len(a) == 0: return None
            lo, hi = float(a.min()), float(a.max())
        else:
            lo = float(np.percentile(a, 0.5))  if len(a) >= 20 else float(a.min())
            hi = float(np.percentile(a, 99.5)) if len(a) >= 20 else float(a.max())
        pad = max((hi - lo) * 0.05, 2.0)
        return [lo - pad, hi + pad]

    traces = []

    if cluster_df is not None and not cluster_df.empty:
        n = cluster_df["n"].values.astype(float)
        sz_log = np.log1p(n)
        sz_norm = (sz_log - sz_log.min()) / max(sz_log.max() - sz_log.min(), 1e-9)
        sizes = np.clip(sz_norm * 28 + 4, 4, 32)
        traces.append(go.Scatter3d(
            x=cluster_df["cx"], y=cluster_df["cz"], z=cluster_df["cy"],
            mode="markers", name="Clusters",
            marker=dict(size=sizes, opacity=0.85, line=dict(width=0), color=n,
                        colorscale="Turbo", cmin=n.min(), cmax=n.max(),
                        colorbar=dict(thickness=12, len=0.5, title="Signs")),
            customdata=np.column_stack([cluster_df["cx"].values, cluster_df["cz"].values, n]),
            hovertemplate="<b>%{customdata[2]:.0f} signs</b><br>X:%{customdata[0]:.0f} Z:%{customdata[1]:.0f}<br>Avg Y:%{z}<extra></extra>",
        ))
        xr = _rng(cluster_df["cx"].values, cap=1_000_000)
        zr = _rng(cluster_df["cz"].values, cap=1_000_000)
        yr = _rng(cluster_df["cy"].values)
    else:
        if not dated_df.empty:
            plot_df = dated_df.dropna(subset=["date_numeric"]).copy() if color_col == "date_numeric" else dated_df.copy()
            if not plot_df.empty:
                dates = plot_df["sign_date"].dt.date.astype(str).tolist() if "sign_date" in plot_df.columns else ["?"] * len(plot_df)
                traces.append(go.Scatter3d(
                    x=plot_df["x"].tolist(), y=plot_df["z"].tolist(), z=plot_df["y"].tolist(),
                    mode="markers", name="Dated", marker=_mk(plot_df, int(marker_size)),
                    customdata=list(zip(plot_df["x"].tolist(), plot_df["y"].tolist(), plot_df["z"].tolist(), plot_df["plain_text"].tolist(), dates)),
                    hovertemplate="<b>%{customdata[3]}</b><br>X:%{customdata[0]} Y:%{customdata[1]} Z:%{customdata[2]}<br>Date:%{customdata[4]}<extra></extra>",
                ))
        if show_undated and not undated_df.empty:
            if color_col != "date_numeric" and color_col in undated_df.columns and full_df is not None and not full_df.empty:
                mk = _mk(undated_df, max(int(marker_size)-1, 3), opacity=0.4, symbol="diamond")
            else:
                mk = dict(size=max(int(marker_size)-1,3), opacity=0.35, line=dict(width=0), symbol="diamond", color="#9ca3af")
            traces.append(go.Scatter3d(
                x=undated_df["x"].tolist(), y=undated_df["z"].tolist(), z=undated_df["y"].tolist(),
                mode="markers", name="No date", marker=mk,
                customdata=list(zip(undated_df["x"].tolist(), undated_df["y"].tolist(), undated_df["z"].tolist(), undated_df["plain_text"].tolist())),
                hovertemplate="<b>%{customdata[3]}</b><br>X:%{customdata[0]} Y:%{customdata[1]} Z:%{customdata[2]}<br>No date<extra></extra>",
            ))
        src = full_df if (full_df is not None and not full_df.empty) else dated_df
        xr = _rng(src["x"].dropna().values if src is not None and not src.empty else None, cap=1_000_000)
        zr = _rng(src["z"].dropna().values if src is not None and not src.empty else None, cap=1_000_000)
        yr = _rng(src["y"].dropna().values if src is not None and not src.empty else None)

    if not traces:
        traces = [go.Scatter3d(x=[], y=[], z=[], mode="markers", marker=dict(size=4, color="grey"), name="No data")]
        xr = zr = yr = None

    fig = go.Figure(data=traces)
    fig.update_layout(
        template=template, paper_bgcolor=bg, plot_bgcolor=bg,
        margin=dict(l=0,r=0,t=0,b=0), uirevision=view_name,
        font=dict(color=text_col),
        legend=dict(orientation="h", x=0, y=1.02, xanchor="left", yanchor="bottom", font=dict(color=text_col)),
        scene=dict(camera=camera, bgcolor=scene_bg,
                   xaxis=_ax("X", xr), yaxis=_ax("Z", zr), zaxis=_ax("Y (Height)", yr)),
    )
    if overlay_title or overlay_date:
        ann = []
        if overlay_title:
            ann.append(dict(
                text=overlay_title, xref="paper", yref="paper",
                x=0.5, y=0.97, showarrow=False, xanchor="center", yanchor="top",
                font=dict(size=22, color=text_col, family="Arial Black"),
            ))
        if overlay_date:
            ann.append(dict(
                text=overlay_date, xref="paper", yref="paper",
                x=0.99, y=0.03, showarrow=False, xanchor="right", yanchor="bottom",
                font=dict(size=18, color=text_col, family="Arial Black"),
                bgcolor="rgba(0,0,0,0.4)", borderpad=5,
            ))
        fig.update_layout(annotations=ann)
    return fig

app = Dash(__name__, external_stylesheets=[LIGHT_THEME], suppress_callback_exceptions=True)
app.title = "2b2t Signs Explorer"
_L = "text-muted small fw-semibold mb-1 d-block"

app.layout = dbc.Container(fluid=True, className="px-3 py-2", children=[
    dbc.Row(className="align-items-center border-bottom pb-2 mb-3 g-2", children=[
        dbc.Col(width="auto", children=[
            html.Span("2b2t Signs Explorer", className="fw-bold fs-5"),
            html.Small(" - 1M WDL", className="text-muted ms-1"),
        ]),
        dbc.Col(width="auto", children=[
            dbc.RadioItems(id="dim-select",
                options=[{"label":v,"value":k} for k,v in DIM_LABELS.items()],
                value="end", inline=True, input_class_name="btn-check",
                label_class_name="btn btn-sm btn-outline-secondary",
                label_checked_class_name="active", class_name="btn-group"),
        ]),
        dbc.Col(width="auto", children=[
            dbc.RadioItems(id="filter-select",
                options=[{"label":"Cody Signs","value":"cody"},{"label":"All Signs","value":"all"}],
                value="cody", inline=True, input_class_name="btn-check",
                label_class_name="btn btn-sm btn-outline-secondary",
                label_checked_class_name="active", class_name="btn-group"),
        ]),
        dbc.Col(className="ms-auto d-flex align-items-center gap-2", width="auto", children=[
            html.Div(id="sign-count", className="text-muted small"),
            html.Span("Light", className="text-muted small"),
            ThemeSwitchAIO(aio_id="theme", themes=[LIGHT_THEME, DARK_THEME]),
            html.Span("Dark", className="text-muted small"),
        ]),
    ]),
    dbc.Row(className="g-3", children=[
        dbc.Col(width=3, children=[
            dbc.Card(className="mb-2 shadow-sm", body=True, children=[
                html.Span("Text Search", className=_L),
                dbc.InputGroup(size="sm", children=[
                    dcc.Input(id="text-search", type="text", debounce=True,
                              placeholder="Filter by text... (Enter to apply)",
                              className="form-control form-control-sm",
                              style={"borderRadius":"4px 0 0 4px"}),
                    dbc.Button("x", id="clear-search", outline=True, color="secondary", size="sm", n_clicks=0),
                ]),
            ]),
            dbc.Card(className="mb-2 shadow-sm", body=True, children=[
                dbc.Row(className="align-items-center g-0 mb-1", children=[
                    dbc.Col(html.Span("Date Range", className=_L + " mb-0")),
                    dbc.Col(dbc.Button("Reset", id="reset-dates", size="sm", n_clicks=0,
                                       color="secondary", outline=True, className="py-0 px-2"), width="auto"),
                ]),
                dcc.DatePickerRange(id="date-range", display_format="YYYY-MM-DD", style={"fontSize":"0.75rem"}),
                html.Small(id="date-hint", className="text-muted fst-italic mt-1 d-block"),
            ]),
            dbc.Card(className="mb-2 shadow-sm", body=True, children=[
                html.Span("Visual", className=_L),
                html.Small("Camera", className="text-muted"),
                dcc.Dropdown(id="view-dropdown", options=[{"label":k,"value":k} for k in VIEWS],
                             value="Default 3D", clearable=False, className="mb-2 mt-1"),
                html.Small("Color By", className="text-muted"),
                dcc.Dropdown(id="color-dropdown", options=[{"label":k,"value":v} for k,v in COLOR_OPTIONS.items()],
                             value="date_numeric", clearable=False, className="mb-2 mt-1"),
                html.Small("Color Scale", className="text-muted"),
                dcc.Dropdown(id="colorscale-dropdown", options=[{"label":s,"value":s} for s in COLORSCALES],
                             value="Viridis", clearable=False, className="mb-2 mt-1"),
                html.Small("Marker Size", className="text-muted"),
                dcc.Slider(id="size-slider", min=2, max=16, step=1, value=5,
                           marks={2:"2",8:"8",16:"16"}, tooltip={"placement":"bottom"}, className="mb-2"),
                dbc.Row(className="align-items-center g-0", children=[
                    dbc.Col(html.Small("Show undated signs", className="text-muted")),
                    dbc.Col(dbc.Switch(id="show-undated", value=True), width="auto"),
                ]),
            ]),
            dbc.Card(className="mb-2 shadow-sm", body=True, children=[
                html.Span("Mode", className=_L),
                dbc.RadioItems(id="display-mode",
                    options=[{"label":"Show All","value":"show_all"},{"label":"Animate","value":"animate"}],
                    value="show_all", inline=True, class_name="small mb-2"),
                html.Hr(className="my-2"),
                # Animate controls
                html.Div(id="anim-controls", style={"display":"none"}, children=[
                    html.Small("Granularity", className="text-muted"),
                    dbc.RadioItems(id="granularity",
                        options=[{"label":"Day","value":"day"},{"label":"Week","value":"week"},{"label":"Month","value":"month"}],
                        value="month", inline=True, class_name="small mb-2 mt-1"),
                    html.Small("Step Mode", className="text-muted"),
                    dbc.RadioItems(id="anim-mode",
                        options=[{"label":"Cumulative","value":"cumulative"},{"label":"Single step","value":"single"}],
                        value="cumulative", inline=True, class_name="small mb-2 mt-1"),
                    dbc.Row(className="g-1 mb-2", children=[
                        dbc.Col(dbc.Button("Play",  id="play-btn",  color="success",   size="sm", className="w-100")),
                        dbc.Col(dbc.Button("Pause", id="pause-btn", color="secondary", size="sm", className="w-100")),
                    ]),
                    html.Small("Speed (ms/step)", className="text-muted"),
                    dcc.Slider(id="speed-slider", min=100, max=2000, step=100, value=600,
                               marks={100:"fast",1000:"med",2000:"slow"}, tooltip={"placement":"bottom"}, className="mb-2"),
                ]),
                html.Div(id="cluster-section", children=[
                    dbc.Row(className="align-items-center g-0 mb-1", children=[
                        dbc.Col(html.Small("Cluster view", className="text-muted")),
                        dbc.Col(dbc.Switch(id="use-clusters", value=False), width="auto"),
                    ]),
                    html.Small("Groups signs into bubbles — size = count.", className="text-muted fst-italic d-block mb-1"),
                    html.Div(id="grid-controls", style={"display":"none"}, children=[
                        html.Small("Grid size", className="text-muted"),
                        dcc.Dropdown(id="grid-size", options=GRID_OPTS, value=512, clearable=False, className="mt-1 mb-1"),
                    ]),
                ]),
                html.Hr(className="my-2"),
                html.Small("Max Sample Points", className="text-muted"),
                dcc.Dropdown(id="max-points", options=MAX_PTS_OPTS, value=5000, clearable=False, className="mt-1"),
            ]),

            dbc.Card(className="mb-2 shadow-sm", body=True, children=[
                html.Span("Export Video", className=_L),
                html.Small("Title overlay", className="text-muted"),
                dcc.Input(id="video-title", type="text", debounce=False,
                          placeholder="e.g.  Cody's Signs - The End",
                          className="form-control form-control-sm mb-2 mt-1"),
                dbc.Row(className="g-2 mb-2", children=[
                    dbc.Col(children=[
                        html.Small("Speed (FPS)", className="text-muted"),
                        dcc.Slider(id="video-fps", min=1, max=30, step=1, value=10,
                                   marks={1:"1",5:"5",10:"10",15:"15",24:"24",30:"30"},
                                   tooltip={"placement":"bottom","always_visible":False},
                                   className="mt-1 mb-0"),
                    ]),
                    dbc.Col(children=[
                        html.Small("Resolution", className="text-muted"),
                        dcc.Dropdown(id="video-res",
                            options=[{"label":"1920x1080","value":"1920x1080"},
                                     {"label":"1280x720", "value":"1280x720"},
                                     {"label":"854x480",  "value":"854x480"}],
                            value="1280x720", clearable=False, className="mt-1"),
                    ]),
                ]),
                html.Small("Granularity", className="text-muted"),
                dbc.RadioItems(id="export-granularity",
                    options=[{"label":"Day","value":"day"},{"label":"Week","value":"week"},{"label":"Month","value":"month"}],
                    value="day", inline=True, class_name="small mb-2 mt-1"),
                html.Small(id="export-frame-est", className="text-muted fst-italic d-block mb-2"),
                dbc.Button("Export & Download", id="export-btn", color="primary",
                           size="sm", className="w-100 mb-2", n_clicks=0),
                html.Small(id="export-status", className="text-muted fst-italic",
                           style={"wordBreak":"break-all"}),
                dcc.Download(id="download-video"),
            ]),
        ]),
        dbc.Col(width=9, children=[
            html.Div(id="cluster-action-bar", style={"display":"none"}, className="mb-2", children=[
                dbc.Alert(color="secondary", className="py-2 mb-0", children=[
                    dbc.Row(className="align-items-center g-2 flex-nowrap", children=[
                        dbc.Col(html.Span(
                            "Select a cluster to zoom into its signs:",
                            className="small text-muted text-nowrap"), width="auto"),
                        dbc.Col(
                            dcc.Dropdown(
                                id="cluster-picker",
                                placeholder="Top clusters by sign count — pick one to drill in…",
                                clearable=True,
                                searchable=True,
                                style={"fontSize":"0.8rem"},
                            ),
                        ),
                    ]),
                ]),
            ]),
            html.Div(id="drill-bar", style={"display":"none"}, className="mb-2", children=[
                dbc.Alert(color="info", className="py-2 mb-0", children=[
                    dbc.Row(className="align-items-center g-2", children=[
                        dbc.Col(html.Span(id="drill-label", className="small fw-semibold")),
                        dbc.Col(
                            dbc.Button("← Back to cluster overview", id="back-btn",
                                       color="secondary", outline=True, size="sm", n_clicks=0),
                            width="auto"),
                    ]),
                ]),
            ]),
            dbc.Card(className="shadow-sm", body=True, style={"padding":"6px"}, children=[
                dbc.Row(className="align-items-center mb-1", children=[
                    dbc.Col(html.Div(id="day-label", className="text-muted small fw-semibold")),
                    dbc.Col(html.Div(id="sample-info", className="text-end text-muted", style={"fontSize":"0.72rem"}), width="auto"),
                ]),
                dcc.Graph(id="scatter3d", style={"height":"78vh"},
                    config={"scrollZoom":True,"displayModeBar":True,"displaylogo":False,
                            "modeBarButtonsToRemove":["resetCameraLastSave3d"]}),
            ]),
        ]),
    ]),
    dcc.Interval(id="anim-interval", interval=600, n_intervals=0, disabled=True),
    dcc.Store(id="frame-store",   data=0),
    dcc.Store(id="dark-mode",     data=False),
    dcc.Store(id="drill-state",   data={"active": False, "cx": None, "cz": None, "gs": 512}),
])

@app.callback(Output("dark-mode","data"), Input(_SWITCH_ID,"value"))
def relay_theme(v): return bool(v) if v is not None else False

@app.callback(Output("text-search","value"), Input("clear-search","n_clicks"), prevent_initial_call=True)
def clear_search(_): return ""

@app.callback(Output("anim-controls","style"), Output("cluster-section","style"), Input("display-mode","value"))
def toggle_mode(mode): return ({},{"display":"none"}) if mode=="animate" else ({"display":"none"},{})

@app.callback(Output("grid-controls","style"), Input("use-clusters","value"))
def toggle_grid(use): return {} if use else {"display":"none"}

@app.callback(
    Output("date-range","min_date_allowed"), Output("date-range","max_date_allowed"),
    Output("date-range","start_date"),       Output("date-range","end_date"),
    Output("date-hint","children"),
    Input("dim-select","value"), Input("filter-select","value"),
    Input("text-search","value"), Input("reset-dates","n_clicks"),
    State("date-range","start_date"), State("date-range","end_date"),
)
def manage_dates(dim, filt, text, _reset, cur_sd, cur_ed):
    try:
        dmin, dmax = fetch_date_range(dim, filt, text)
        if not dmin:
            return None, None, None, None, "No dated signs in this dataset"
        if ctx.triggered_id in {"dim-select","filter-select","text-search","reset-dates"} or not cur_sd:
            sd, ed, hint = dmin, dmax, "Showing full data range"
        else:
            sd   = cur_sd or dmin
            ed   = cur_ed or dmax
            hint = "Showing full data range" if (sd == dmin and ed == dmax) else f"{sd}  to  {ed}"
        return dmin, dmax, sd, ed, hint
    except Exception:
        print(traceback.format_exc())
        return None, None, None, None, "Date range unavailable"

@app.callback(
    Output("anim-interval","disabled"), Output("anim-interval","interval"),
    Input("play-btn","n_clicks"), Input("pause-btn","n_clicks"), Input("speed-slider","value"),
    Input("dim-select","value"), Input("filter-select","value"), Input("display-mode","value"),
    State("anim-interval","disabled"), prevent_initial_call=True,
)
def toggle_anim(play, pause, speed, dim, filt, mode, disabled):
    t = ctx.triggered_id
    if t in {"dim-select","filter-select","display-mode"}: return True, speed
    if t == "play-btn":  return False, speed
    if t == "pause-btn": return True,  speed
    return disabled, speed

@app.callback(
    Output("frame-store","data"),
    Input("anim-interval","n_intervals"),
    Input("dim-select","value"), Input("filter-select","value"),
    Input("display-mode","value"), Input("granularity","value"),
    State("frame-store","data"), State("date-range","start_date"), State("date-range","end_date"),
    State("max-points","value"), State("text-search","value"),
    prevent_initial_call=True,
)
def advance_frame(_, dim, filt, mode, gran, frame, sd, ed, max_pts, text):
    if ctx.triggered_id in {"dim-select","filter-select","display-mode","granularity"}: return 0
    try:
        df, _, _ = fetch_signs(dim, filt, text, 5000 if max_pts is None else int(max_pts))
        if df.empty: return 0
        dated = _anim_clip(df[df["sign_date"].notna()].copy())
        if dated.empty: return 0
        sd_d = pd.to_datetime(sd).date() if sd else dated["sign_date"].dt.date.min()
        ed_d = pd.to_datetime(ed).date() if ed else dated["sign_date"].dt.date.max()
        steps = anim_steps(dated, gran, sd_d, ed_d)
        return 0 if not steps else (int(frame)+1) % len(steps)
    except Exception:
        print(traceback.format_exc()); return 0

@app.callback(
    Output("drill-state","data"),
    Input("cluster-picker","value"),
    Input("back-btn","n_clicks"),
    Input("use-clusters","value"),
    Input("dim-select","value"),
    Input("filter-select","value"),
    Input("text-search","value"),
    State("grid-size","value"),
    prevent_initial_call=True,
)
def auto_drill(picker_val, _back, use_clusters, dim, filt, _text, grid_size):
    if not ctx.triggered:
        return no_update
    gs = int(grid_size or 512)
    null = {"active": False, "cx": None, "cz": None, "gs": gs}
    trigger = ctx.triggered[0]["prop_id"].split(".")[0]

    if trigger in {"back-btn", "dim-select", "filter-select", "text-search"} or not use_clusters:
        return null

    if trigger == "cluster-picker" and picker_val:
        try:
            cx, cz = (float(v) for v in picker_val.split("|"))
            return {"active": True, "cx": cx, "cz": cz, "gs": gs}
        except Exception:
            pass

    return no_update

@app.callback(
    Output("drill-bar","style"),          Output("drill-label","children"),
    Output("cluster-action-bar","style"), Output("use-clusters","style"),
    Input("drill-state","data"),          Input("use-clusters","value"),
)
def update_drill_bar(drill, use_clusters):
    drilled = bool(drill and drill.get("active"))
    if drilled:
        cx, cz, gs = drill["cx"], drill["cz"], drill["gs"]
        label = (f"Signs near  X {cx:,.0f}  Z {cz:,.0f}  "
                 f"(±{int(gs * 1.5):,} blocks)")
        return {}, label, {"display": "none"}, {}
    action_style = {} if use_clusters else {"display": "none"}
    return {"display": "none"}, "", action_style, {}

@app.callback(
    Output("cluster-picker","options"),
    Input("use-clusters","value"),
    Input("dim-select","value"),
    Input("filter-select","value"),
    Input("text-search","value"),
    Input("grid-size","value"),
)
def populate_cluster_picker(use_clusters, dim, filt, text, grid_size):

    if not use_clusters:
        return []
    try:
        gs = int(grid_size or 512)
        cdf = fetch_clusters(dim, filt, (text or "").strip(), gs)
        if cdf.empty:
            return []
        top  = cdf.nlargest(30, "n")
        return [
            {"label": f"X {int(r.cx):,}  Z {int(r.cz):,}  —  {int(r.n):,} signs",
             "value": f"{r.cx}|{r.cz}"}
            for r in top.itertuples()
        ]
    except Exception:
        print(traceback.format_exc())
        return []

@app.callback(
    Output("scatter3d","figure"), Output("day-label","children"),
    Output("sign-count","children"), Output("sample-info","children"),
    Input("frame-store","data"),       Input("view-dropdown","value"),
    Input("color-dropdown","value"),   Input("colorscale-dropdown","value"),
    Input("size-slider","value"),      Input("anim-mode","value"),
    Input("granularity","value"),      Input("date-range","start_date"),
    Input("date-range","end_date"),    Input("dim-select","value"),
    Input("filter-select","value"),    Input("text-search","value"),
    Input("max-points","value"),       Input("show-undated","value"),
    Input("display-mode","value"),     Input("use-clusters","value"),
    Input("grid-size","value"),        Input("dark-mode","data"),
    Input("video-title","value"),      Input("drill-state","data"),
)
def update_plot(frame, view, color_col, colorscale, msize, anim_mode, gran,
                sd, ed, dim, filt, text, max_pts, show_undated, mode,
                use_clusters, grid_size, dark, vid_title, drill):
    try:
        cam = VIEWS.get(view, VIEWS["Default 3D"])
        text = (text or "").strip()
        max_pts = 5000 if max_pts is None else int(max_pts)
        title = (vid_title or "").strip() or None
        _empty = lambda: build_figure(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                                       color_col, colorscale, msize, cam, dark, show_undated, view)

        if mode == "show_all" and use_clusters:
            gs = int(grid_size or 512)

            if drill and drill.get("active"):
                cx, cz = drill["cx"], drill["cz"]
                area_df = fetch_cluster_area(dim, filt, text, cx, cz, gs)
                if area_df.empty:
                    return (_empty(), f"No signs near X={cx:.0f} Z={cz:.0f}", "0 signs",
                            f"drill X={cx:.0f} Z={cz:.0f}")

                dtd_a = area_df[area_df["sign_date"].notna()].copy()
                und_a = area_df[area_df["sign_date"].isna()].copy()
                fig = build_figure(dtd_a, und_a, area_df, color_col, colorscale,
                                     msize, cam, dark, show_undated, view)
                n_u   = len(und_a) if show_undated else 0
                return (fig,
                        f"Drilled: X={cx:,.0f} Z={cz:,.0f} | {DIM_LABELS[dim]}",
                        f"{len(dtd_a):,} dated  {n_u:,} undated",
                        f"{len(area_df):,} signs in cluster area")
            cdf = fetch_clusters(dim, filt, text, gs)
            if cdf.empty: return _empty(), "No clusters", "0 signs", f"grid={gs}"
            fig = build_figure(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                               color_col, colorscale, msize, cam, dark, show_undated, view, cluster_df=cdf)
            return (fig, f"Cluster view | grid={gs} | {DIM_LABELS[dim]} — click a bubble to drill in",
                    f"{len(cdf):,} clusters ~{int(cdf['n'].sum()):,} signs", f"{len(cdf):,} cells")

        df, total, fetched = fetch_signs(dim, filt, text, max_pts)
        info = f"{fetched:,} of {total:,} shown (sampled)" if fetched < total else f"All {fetched:,} signs"
        if df.empty: return _empty(), "No signs matched", "0 signs", info


        dtd = df[df["sign_date"].notna()].copy()
        undtd = df[df["sign_date"].isna()].copy()

        if mode == "show_all":
            fig = build_figure(dtd, undtd, df, color_col, colorscale, msize, cam, dark, show_undated, view)
            n_u = len(undtd) if show_undated else 0
            return fig, f"All Signs | {DIM_LABELS[dim]}", f"{len(dtd):,} dated  {n_u:,} undated", info

        dtd = _anim_clip(dtd)
        sd_d = pd.to_datetime(sd).date() if sd else (dtd["sign_date"].dt.date.min() if not dtd.empty else None)
        ed_d = pd.to_datetime(ed).date() if ed else (dtd["sign_date"].dt.date.max() if not dtd.empty else None)
        steps = anim_steps(dtd, gran, sd_d, ed_d) if (sd_d and ed_d) else []
        if not steps:
            return _empty(), "No dated signs in range", f"0 dated  {len(undtd) if show_undated else 0:,} undated", info

        frame = int(frame) % len(steps)
        step  = steps[frame]
        if anim_mode == "cumulative":
            sub, lbl = dtd[dtd["sign_date"].dt.date <= step].copy(), f"Up to {step}"
        elif gran == "day":
            sub, lbl = dtd[dtd["sign_date"].dt.date == step].copy(), f"Day: {step}"
        elif gran == "week":
            sub = dtd[(dtd["sign_date"].dt.date >= step) & (dtd["sign_date"].dt.date <= step + pd.Timedelta(days=6))].copy()
            lbl = f"Week of {step}"
        else:
            sub = dtd[dtd["sign_date"].dt.to_period("M") == pd.Period(str(step),"M")].copy()
            lbl = f"Month: {pd.Timestamp(step).strftime('%b %Y')}"

        fig = build_figure(sub, undtd, dtd, color_col, colorscale, msize, cam, dark, show_undated, view,
                           overlay_title=title, overlay_date=lbl)
        return fig, f"{lbl}  ({len(sub):,} signs)", f"Step {frame+1}/{len(steps)} | {DIM_LABELS[dim]}", info

    except Exception:
        print("=== ERROR ===\n", traceback.format_exc())
        return go.Figure(), "ERROR - check console", "Error", "An error occurred — see server console for details."


@app.callback(
    Output("export-frame-est","children"),
    Input("export-granularity","value"),
    Input("date-range","start_date"), Input("date-range","end_date"),
    State("dim-select","value"), State("filter-select","value"),
    State("text-search","value"),
)
def update_frame_estimate(gran, sd, ed, dim, filt, text):
    try:
        df, _, _ = fetch_signs(dim, filt, (text or "").strip(), 50_000)
        if df.empty:
            return ""
        dtd = _anim_clip(df[df["sign_date"].notna()].copy())
        sd_d = pd.to_datetime(sd).date() if sd else (dtd["sign_date"].dt.date.min() if not dtd.empty else None)
        ed_d = pd.to_datetime(ed).date() if ed else (dtd["sign_date"].dt.date.max() if not dtd.empty else None)
        steps = anim_steps(dtd, gran, sd_d, ed_d) if (sd_d and ed_d) else []
        if not steps:
            return "No dated signs in range"
        secs = len(steps) * 3
        est = f"{secs//60}m {secs%60}s" if secs >= 60 else f"~{secs}s"
        return f"{len(steps)} frames · est. render time {est}"
    except Exception:
        return ""


@app.callback(
    Output("export-status","children"),
    Output("download-video","data"),
    Input("export-btn","n_clicks"),
    State("video-title","value"),   State("video-fps","value"),
    State("video-res","value"),
    State("dim-select","value"),    State("filter-select","value"),
    State("text-search","value"),   State("max-points","value"),
    State("export-granularity","value"), State("anim-mode","value"),
    State("date-range","start_date"), State("date-range","end_date"),
    State("view-dropdown","value"), State("color-dropdown","value"),
    State("colorscale-dropdown","value"), State("size-slider","value"),
    State("show-undated","value"),  State("dark-mode","data"),
    prevent_initial_call=True,
)
def export_video(n_clicks, vid_title, fps, resolution,
                 dim, filt, text, max_pts, gran, anim_mode, sd, ed,
                 view, color_col, colorscale, msize, show_undated, dark):
    try:
        df, _, _ = fetch_signs(dim, filt, (text or "").strip(), 5000 if max_pts is None else int(max_pts))
        if df.empty:
            return "No data to export", None
        dtd = _anim_clip(df[df["sign_date"].notna()].copy())
        undtd = df[df["sign_date"].isna()].copy()
        sd_d = pd.to_datetime(sd).date() if sd else (dtd["sign_date"].dt.date.min() if not dtd.empty else None)
        ed_d = pd.to_datetime(ed).date() if ed else (dtd["sign_date"].dt.date.max() if not dtd.empty else None)
        steps = anim_steps(dtd, gran, sd_d, ed_d) if (sd_d and ed_d) else []
        if not steps:
            return "No dated signs found in the current range", None

        w, h   = (int(x) for x in (resolution or "1280x720").split("x"))
        cam    = VIEWS.get(view, VIEWS["Default 3D"])
        title  = (vid_title or "").strip() or None
        fps_val = int(fps or 10)
        tmpdir = tempfile.mkdtemp()

        def _sub_for_step(step):
            if anim_mode == "cumulative":
                return dtd[dtd["sign_date"].dt.date <= step].copy(), str(step)
            if gran == "day":
                return dtd[dtd["sign_date"].dt.date == step].copy(), str(step)
            if gran == "week":
                end_w = step + pd.Timedelta(days=6)
                return dtd[(dtd["sign_date"].dt.date >= step) & (dtd["sign_date"].dt.date <= end_w)].copy(), f"Week of {step}"
            sub = dtd[dtd["sign_date"].dt.to_period("M") == pd.Period(str(step), "M")].copy()
            return sub, pd.Timestamp(step).strftime("%B %Y")

        from playwright.sync_api import sync_playwright
        _browser_exe = _find_browser()

        frame_paths = []
        with sync_playwright() as pw:
            if _browser_exe:
                browser = pw.chromium.launch(executable_path=_browser_exe, headless=True)
            else:
                browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": w, "height": h})
            for i, step in enumerate(steps):
                sub, lbl = _sub_for_step(step)
                fig = build_figure(sub, undtd, dtd, color_col, colorscale, msize,
                                   cam, dark, show_undated, view,
                                   overlay_title=title, overlay_date=lbl)
                fig.update_layout(width=w, height=h)
                page.set_content(_frame_html(fig, w, h))
                page.wait_for_timeout(1500)
                p = os.path.join(tmpdir, f"frame_{i:05d}.png")
                page.screenshot(path=p, full_page=False)
                frame_paths.append(p)
                if (i + 1) % 5 == 0:
                    print(f"[export] {i+1}/{len(steps)} frames rendered")
            browser.close()

        suffix = ".mp4"
        out = os.path.join(tempfile.gettempdir(), f"signs_export{suffix}")
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe, "-y",
                "-framerate", str(fps_val),
                "-i", os.path.join(tmpdir, "frame_%05d.png"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", out,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-600:])
            print(f"[export] MP4 OK")
        except Exception as mp4_err:
            print(f"[export] MP4 failed ({mp4_err}), falling back to GIF")
            from PIL import Image as PILImage
            suffix = ".gif"
            out = os.path.join(tempfile.gettempdir(), f"signs_export{suffix}")
            imgs = [PILImage.open(p) for p in frame_paths]
            imgs[0].save(out, save_all=True, append_images=imgs[1:],
                         duration=int(1000 / fps_val), loop=0, optimize=False)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        with open(out, "rb") as f:
            video_bytes = f.read()
        os.unlink(out)

        dl_name = f"signs_{dim}_{gran}{suffix}"
        return (f"Done — {len(steps)} frames · downloading as {dl_name}",
                dcc.send_bytes(video_bytes, dl_name))
    except Exception as e:
        print(traceback.format_exc())
        return f"Export failed: {str(e)[:200]}", None

app.run(
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8050)),
    debug=os.environ.get("DASH_DEBUG", "false").lower() == "true",
)