from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # .env があれば環境変数として読み込む(既存の環境変数が優先)

# dataviz 標準カテゴリカルパレット(スロット順は固定・循環させない)
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                 "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
                "#d55181", "#008300", "#9085e9", "#e66767"]

RESAMPLE_RULES = {"生データ": None, "1分": "1min", "5分": "5min",
                  "10分": "10min", "1時間": "1h"}

MAX_ROWS = 100_000

st.set_page_config(page_title="音環境モニター", page_icon="🔊", layout="wide")


# ------------------------------------------------------------------- D1 API
class D1Error(RuntimeError):
    pass


def _extract_rows(payload) -> list[dict]:
    """Cloudflare D1 API / 自前 Worker のどちらのレスポンス形でも行を取り出す。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise D1Error(f"予期しないレスポンス形式です: {type(payload).__name__}")

    if payload.get("success") is False or payload.get("errors"):
        errs = payload.get("errors") or [{"message": "unknown error"}]
        msgs = [e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errs]
        raise D1Error("; ".join(msgs))

    result = payload.get("result", payload)
    if isinstance(result, list):
        if not result:
            return []
        first = result[0]
        if isinstance(first, dict) and "results" in first:
            return first["results"] or []
        return result
    if isinstance(result, dict) and "results" in result:
        return result["results"] or []
    raise D1Error("レスポンスに results が含まれていません。")


@st.cache_data(show_spinner=False, ttl=300)
def d1_query(endpoint: str, token: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        res = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"sql": sql, "params": list(params)},
            timeout=60,
        )
    except requests.RequestException as e:
        raise D1Error(f"接続に失敗しました: {e}") from e

    if res.status_code >= 400:
        body = res.text[:300]
        raise D1Error(f"HTTP {res.status_code}: {body}")
    try:
        payload = res.json()
    except ValueError as e:
        raise D1Error(f"JSON として解釈できません: {res.text[:200]}") from e

    return pd.DataFrame(_extract_rows(payload))


# ------------------------------------------------------------------ 音響計算
def energy_mean(levels) -> float:
    """エネルギー平均 (等価騒音レベル)。単純平均ではなく 10*log10(mean(10^(L/10)))。"""
    a = np.asarray(levels, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return float("nan")
    return float(10.0 * np.log10(np.mean(np.power(10.0, a / 10.0))))


def percentile_level(levels, n: int) -> float:
    """時間率騒音レベル LN(全体の N% の時間で超える値)。"""
    a = np.asarray(levels, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return float("nan")
    return float(np.percentile(a, 100 - n))


def resample_leq(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (df.set_index("dt")
              .groupby("device")["laeq"]
              .resample(rule)
              .agg(energy_mean)
              .rename("laeq")
              .reset_index()
              .dropna(subset=["laeq"]))


# -------------------------------------------------------------------- 接続設定
endpoint = os.environ.get("D1_ENDPOINT", "").strip()
token = os.environ.get("D1_TOKEN", "").strip()

if not endpoint or not token:
    missing = [k for k, v in (("D1_ENDPOINT", endpoint), ("D1_TOKEN", token)) if not v]
    st.title("🔊 音環境モニター")
    st.error(f"環境変数が設定されていません: {', '.join(missing)}")
    st.code("export D1_ENDPOINT='https://api.cloudflare.com/client/v4/accounts/"
            "<account_id>/d1/database/<database_id>/query'\n"
            "export D1_TOKEN='<API token>'\n"
            "streamlit run app.py", language="bash")
    st.caption("同じ内容を `.env` に書いておけば起動時に自動で読み込まれます"
               "（`.env` は .gitignore 済み）。")
    st.stop()


def run(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return d1_query(endpoint, token, sql, params)
    except D1Error as e:
        st.error(f"クエリに失敗しました: {e}")
        st.stop()


tables_df = run("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY name")
tables = tables_df["name"].tolist() if not tables_df.empty else []
if not tables:
    st.error("テーブルが見つかりません。トークンの権限とデータベースを確認してください。")
    st.stop()


@st.cache_data(show_spinner=False, ttl=300)
def get_columns(endpoint: str, token: str, table: str) -> list[str]:
    df = d1_query(endpoint, token, f'SELECT * FROM "{table}" LIMIT 1')
    return list(df.columns)


default_table = next((t for t in tables if "laeq" in
                      [c.lower() for c in get_columns(endpoint, token, t)]), tables[0])
st.sidebar.header("データ")
st.sidebar.caption(f"接続先: {urlparse(endpoint).netloc or endpoint}")
table = st.sidebar.selectbox("テーブル", tables, index=tables.index(default_table))
columns = get_columns(endpoint, token, table)
if not columns:
    st.error(f"`{table}` に行がありません。")
    st.stop()


def pick(cands: list[str], fallback: int = 0) -> int:
    lower = [c.lower() for c in columns]
    for c in cands:
        if c in lower:
            return lower.index(c)
    return min(fallback, len(columns) - 1)


with st.sidebar.expander("列の対応", expanded=False):
    dt_col = st.selectbox("日時", columns, index=pick(["datetime", "timestamp", "date_time", "time"]))
    laeq_col = st.selectbox("騒音レベル", columns, index=pick(["laeq", "leq", "level", "db"], 1))
    _dev_default = next((c for c in columns if c.lower() in ("device_id", "device")), None)
    dev_col = st.selectbox("デバイス", ["(なし)"] + columns,
                           index=columns.index(_dev_default) + 1 if _dev_default else 0)
    dev_col = None if dev_col == "(なし)" else dev_col

# 記録期間
rng_df = run(f'SELECT MIN("{dt_col}") AS lo, MAX("{dt_col}") AS hi FROM "{table}"')
lo_raw, hi_raw = (rng_df.iloc[0]["lo"], rng_df.iloc[0]["hi"]) if not rng_df.empty else (None, None)
if lo_raw is None or pd.isna(lo_raw):
    st.error("データが空です。")
    st.stop()
min_d, max_d = pd.to_datetime(lo_raw).date(), pd.to_datetime(hi_raw).date()

st.sidebar.header("期間")
mode = st.sidebar.radio("指定方法", ["単日", "期間"], horizontal=True)
if mode == "単日":
    d = st.sidebar.date_input("日付", value=max_d, min_value=min_d, max_value=max_d)
    start_d = end_d = d
else:
    default_start = max(min_d, max_d - timedelta(days=6))
    rng = st.sidebar.date_input("開始 – 終了", value=(default_start, max_d),
                                min_value=min_d, max_value=max_d)
    if isinstance(rng, tuple) and len(rng) == 2:
        start_d, end_d = rng
    else:
        start_d = end_d = rng if isinstance(rng, date) else max_d
st.sidebar.caption(f"記録期間: {min_d} 〜 {max_d}")

st.sidebar.header("表示")
agg_label = st.sidebar.select_slider("集計単位", list(RESAMPLE_RULES), value="生データ")
rule = RESAMPLE_RULES[agg_label]
theme = st.sidebar.radio("テーマ", ["ライト", "ダーク"], horizontal=True)
dark = theme == "ダーク"
palette = PALETTE_DARK if dark else PALETTE_LIGHT
template = "plotly_dark" if dark else "plotly_white"
grid = "rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.10)"
if st.sidebar.button("再読み込み", width="stretch"):
    st.cache_data.clear()
    st.rerun()

# -------------------------------------------------------------------- データ
sel_cols = [f'"{dt_col}" AS dt', f'"{laeq_col}" AS laeq']
if dev_col:
    sel_cols.append(f'"{dev_col}" AS device')
lo_s = datetime.combine(start_d, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
hi_s = datetime.combine(end_d + timedelta(days=1), datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")

raw = run(f'SELECT {", ".join(sel_cols)} FROM "{table}" '
          f'WHERE "{dt_col}" >= ? AND "{dt_col}" < ? '
          f'ORDER BY "{dt_col}" LIMIT {MAX_ROWS}', (lo_s, hi_s))

st.title("🔊 音環境モニター")
period = f"{start_d}" if start_d == end_d else f"{start_d} 〜 {end_d}"
st.caption(f"`{table}` / {period}")

if raw.empty:
    st.info("指定した期間にデータがありません。")
    st.stop()

df = raw.copy()
df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
df["laeq"] = pd.to_numeric(df["laeq"], errors="coerce")
df = df.dropna(subset=["dt", "laeq"])
if "device" not in df.columns:
    df["device"] = "all"
df["device"] = df["device"].astype(str)

if len(raw) >= MAX_ROWS:
    st.warning(f"取得件数が上限 {MAX_ROWS:,} 件に達しました。期間を短くするか集計単位を粗くしてください。")

devices = sorted(df["device"].unique(), key=lambda x: (len(x), x))
if dev_col:
    sel = st.sidebar.multiselect("デバイス", devices, default=devices)
    if not sel:
        st.info("デバイスを 1 つ以上選択してください。")
        st.stop()
    df = df[df["device"].isin(sel)]
    devices = [d for d in devices if d in sel]

color_of = {d: palette[i % len(palette)] for i, d in enumerate(devices)}

# ------------------------------------------------------------------ 指標タイル
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("LAeq", f"{energy_mean(df['laeq']):.1f} dB")
c2.metric("LAmax", f"{df['laeq'].max():.1f} dB")
c3.metric("L5", f"{percentile_level(df['laeq'], 5):.1f} dB")
c4.metric("L50", f"{percentile_level(df['laeq'], 50):.1f} dB")
c5.metric("件数", f"{len(df):,}")

# ---------------------------------------------------------------- 時系列
plot_df = resample_leq(df, rule) if rule else df[["dt", "device", "laeq"]]

fig = go.Figure()
for d in devices:
    sub = plot_df[plot_df["device"] == d]
    if sub.empty:
        continue
    fig.add_trace(go.Scatter(
        x=sub["dt"], y=sub["laeq"], name=str(d),
        mode="lines+markers" if len(sub) < 200 else "lines",
        line=dict(color=color_of[d], width=2),
        marker=dict(size=8, color=color_of[d]),
        hovertemplate="%{x|%Y-%m-%d %H:%M:%S}<br>%{y:.1f} dB<extra>" + str(d) + "</extra>",
    ))
fig.update_layout(
    template=template, height=420, hovermode="x unified",
    margin=dict(l=8, r=8, t=44, b=8),
    title="時系列",
    showlegend=len(devices) >= 2,
    legend=dict(orientation="h", y=1.12, x=0, title=None),
    yaxis=dict(title="LAeq [dB]", gridcolor=grid, zeroline=False),
    xaxis=dict(title=None, gridcolor=grid, showspikes=True, spikemode="across",
               spikethickness=1, spikedash="dot"),
)
st.plotly_chart(fig, width="stretch")

# ------------------------------------------------- 時間帯別 + デバイス別サマリ
left, right = st.columns(2)

hourly = (df.assign(hour=df["dt"].dt.hour)
            .groupby(["device", "hour"])["laeq"].agg(energy_mean).reset_index())
fh = go.Figure()
for d in devices:
    sub = hourly[hourly["device"] == d]
    fh.add_trace(go.Bar(x=sub["hour"], y=sub["laeq"], name=str(d),
                        marker=dict(color=color_of[d]),
                        hovertemplate="%{x}時<br>%{y:.1f} dB<extra>" + str(d) + "</extra>"))
fh.update_layout(template=template, height=340, barmode="group", bargap=0.25,
                 margin=dict(l=8, r=8, t=44, b=8), title="時間帯別",
                 showlegend=len(devices) >= 2,
                 legend=dict(orientation="h", y=1.15, x=0, title=None),
                 xaxis=dict(title="時刻", dtick=2, gridcolor="rgba(0,0,0,0)"),
                 yaxis=dict(title="LAeq [dB]", gridcolor=grid, zeroline=False))
left.plotly_chart(fh, width="stretch")

stats = (df.groupby("device")["laeq"]
           .agg(LAeq=energy_mean, LAmax="max", LAmin="min",
                L5=lambda s: percentile_level(s, 5),
                L50=lambda s: percentile_level(s, 50),
                L95=lambda s: percentile_level(s, 95),
                件数="count")
           .round(1)
           .reindex(devices))
right.markdown("**デバイス別サマリ**")
right.dataframe(stats, width="stretch")

# ------------------------------------------------------------------ データ表
with st.expander(f"データ表示（{len(df):,} 件）"):
    view = df.rename(columns={"dt": "日時", "laeq": "LAeq [dB]", "device": "デバイス"})
    if not dev_col:
        view = view.drop(columns=["デバイス"])
    st.dataframe(view, width="stretch", height=360)
    st.download_button("CSV ダウンロード",
                       view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"laeq_{start_d}_{end_d}.csv", mime="text/csv")
