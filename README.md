# sound_level_loger

騒音ロガー（Cloudflare D1）のデータを日付指定で可視化する Streamlit アプリ。

## セットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

接続情報は環境変数で渡す:

```bash
export D1_ENDPOINT='https://api.cloudflare.com/client/v4/accounts/<account_id>/d1/database/<database_id>/query'
export D1_TOKEN='<API token>'
```

`.env` に書いておけば起動時に自動で読み込まれる（`.gitignore` 済み）:

```bash
cp .env.example .env   # 値を埋める
```

データ取得はすべて D1 の HTTP API（`POST {"sql": ..., "params": [...]}`）経由。
自前の Worker をプロキシにしている場合は、その URL を `D1_ENDPOINT` に指定する
（D1 の JSON をそのまま返す形、`{"results": [...]}`、素の配列のいずれでも読める）。

## 起動

```bash
streamlit run app.py
```

## 機能

- **日付指定**: 単日 / 期間のどちらかを選択。DB の実データ範囲を上下限に設定
- デバイス（`device_id`）で絞り込み、集計単位は 生データ / 1・5・10分 / 1時間
- LAeq 時系列、時間帯別 LAeq、デバイス別サマリ（LAeq・LAmax・LAmin・L5・L50・L95）
- CSV ダウンロード

平均は単純平均ではなくエネルギー平均 `10·log10(mean(10^(L/10)))`、
LN は時間率騒音レベル（全体の N% の時間で超える値）で計算している。

## テーブル / 列

`id / datetime / laeq / device_id` を想定。テーブルと列はサイドバーの
「列の対応」で変更できるので、命名が違っても対応可能。
