"""
適時開示（TDnet）の取得・採点・保存

  - 取得元（DisclosureSource）
      CsvDisclosureSource   : フォルダ内の YYYY-MM-DD.csv を読む（手動・検証用）
      JQuantsTdnetSource    : J-Quants API の TDnet アドオン（2026年5月〜、個人向け）
                              ※ エンドポイント・項目名は契約後に J-Quants の仕様書で確認し、
                                 DS_JQ_* の環境変数で合わせること（下記 from_env 参照）
  - 採点
      rule_score()          : タイトル（と本文があれば本文）からルールで -3〜+3 の点数をつける
      ClaudeScorer          : 任意。Claude API で内容を読んで点数をつける（DS_LLM_ENABLED=true）
  - 保存（DisclosureStore）
      state/disclosures/YYYY-MM-DD.json に1日分ずつ保存する。B（開示採点）と C（材料なし下落の戻り）で共用
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KABUCOM_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORE_DIR = os.path.join(KABUCOM_DIR, "state", "disclosures")


@dataclass
class Disclosure:
    code: str             # 4桁（または5桁）の銘柄コード
    name: str
    disclosed_at: str     # ISO 形式の日時
    title: str
    body: str = ""        # 本文（取れる場合だけ。採点の精度が上がる）
    url: str = ""
    category: str = ""
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    scored_by: str = ""

    @property
    def time(self) -> datetime:
        return datetime.fromisoformat(self.disclosed_at)


def normalize_code(code) -> str:
    """J-Quants などの5桁コード（末尾0）を4桁にそろえる"""
    s = str(code or "").strip()
    return s[:4] if len(s) == 5 and s.endswith("0") else s


# ---------------------------------------------------------------------------
# ルールによる採点
# ---------------------------------------------------------------------------

# (カテゴリ, 点数, タイトルの正規表現)。上から順に最初に当たったものを使う
_RULES: List[Tuple[str, float, str]] = [
    ("継続企業の前提", -3, r"継続企業の前提"),
    ("決算発表の延期", -3, r"(決算発表|決算短信).*(延期|遅延)|開示.*延期"),
    ("不適切会計", -3, r"不適切な会計|不正|第三者委員会|調査委員会"),
    ("MSCB・行使価額修正", -3, r"行使価額修正条項|MSCB|転換価額修正"),
    ("公募増資・売出し", -2, r"新株式発行|公募|売出し|募集新株予約権|第三者割当"),
    ("下方修正", -2, r"下方修正"),
    ("減配", -2, r"減配|無配"),
    ("優待廃止", -2, r"(株主優待|優待).*(廃止|中止)"),
    ("特別損失", -1, r"特別損失|減損"),
    ("TOB（対象）", 2, r"公開買付けに関する意見表明|公開買付けの開始|MBO|マネジメント・バイアウト"),
    ("優待新設", 2, r"(株主優待|優待).*(新設|導入)"),
    ("上方修正", 2, r"上方修正"),
    ("自己株式取得", 1, r"自己株式(の)?取得|自社株買い|自己株式の公開買付け"),
    ("増配", 1, r"増配|記念配当|配当予想の修正"),
    ("優待拡充", 1, r"(株主優待|優待).*(拡充|変更)"),
    ("株式分割", 0.5, r"株式分割"),
    ("業績予想の修正", 0, r"業績予想の修正|業績予想及び配当予想の修正"),
    ("決算", 0, r"決算短信|決算説明"),
]

_BUYBACK_PCT = re.compile(r"(発行済株式総数|発行済株式).{0,40}?割合.{0,10}?([0-9]+(?:\.[0-9]+)?)\s*[%％]")
_REVISION_PCT = re.compile(r"(営業利益|経常利益).{0,80}?増減率.{0,40}?([+\-－△▲]?[0-9]+(?:\.[0-9]+)?)\s*[%％]")


def rule_score(title: str, body: str = "") -> Tuple[str, float, List[str]]:
    """(カテゴリ, 点数, 理由) を返す。本文があれば自社株買いの規模・修正率で点数を調整する"""
    text = title or ""
    category, score, reasons = "その他", 0.0, []
    for cat, pts, pattern in _RULES:
        if re.search(pattern, text):
            category, score = cat, pts
            reasons.append(f"タイトルが「{cat}」に該当")
            break
    if body:
        if category == "自己株式取得":
            m = _BUYBACK_PCT.search(body)
            if m:
                pct = float(m.group(2))
                bonus = 2 if pct >= 5 else 1 if pct >= 2 else 0
                score += bonus
                reasons.append(f"取得上限が発行済株式の {pct}%")
        if category in ("上方修正", "業績予想の修正", "下方修正"):
            m = _REVISION_PCT.search(body)
            if m:
                pct = float(m.group(2).replace("－", "-").replace("△", "-").replace("▲", "-"))
                adj = 2 if pct >= 30 else 1 if pct >= 10 else -2 if pct <= -10 else 0
                if category == "業績予想の修正":
                    score = adj
                else:
                    score += adj / 2
                reasons.append(f"利益の修正率 {pct:+.1f}%")
    return category, max(-3.0, min(3.0, score)), reasons


# ---------------------------------------------------------------------------
# Claude による採点（任意）
# ---------------------------------------------------------------------------

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["category", "score", "reason"],
    "additionalProperties": False,
}

_LLM_SYSTEM = (
    "あなたは日本株の適時開示を評価するアナリストです。与えられた開示について、"
    "翌営業日以降の株価への影響（市場全体に対する超過リターン）を -3〜+3 の点数で評価してください。"
    "+3 は大幅な上昇が見込まれる強い好材料、0 は影響なし、-3 は大幅な下落が見込まれる強い悪材料です。"
    "業績修正は修正率と通期予想に対する進捗、自己株式取得は発行済株式に対する規模と取得方法、"
    "株主優待は新設か拡充か改悪か、増資は希薄化の大きさを重視してください。"
    "タイトルだけで判断できない場合は 0 に近い点数にしてください。"
    "category は開示の種類を短い日本語で、reason は判断理由を1〜2文で書いてください。"
)


class ClaudeScorer:
    """Claude API で開示を採点する。anthropic パッケージと ANTHROPIC_API_KEY が必要"""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", max_body_chars: int = 20000):
        import anthropic  # 任意の依存なので使うときだけ読み込む
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_body_chars = max_body_chars

    def score(self, d: Disclosure) -> Optional[Tuple[str, float, str]]:
        content = f"銘柄: {d.code} {d.name}\n開示日時: {d.disclosed_at}\nタイトル: {d.title}\n"
        if d.body:
            # 長い本文は先頭だけ渡す（サマリー情報は先頭にあることが多い）。切り詰めたことは明示する
            body = d.body[: self.max_body_chars]
            note = "（本文が長いため先頭のみ）" if len(d.body) > self.max_body_chars else ""
            content += f"本文{note}:\n{body}\n"
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=_LLM_SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": _LLM_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self.anthropic.RateLimitError as e:
            logger.warning(f"Claude API のレート制限: {e}")
            return None
        except self.anthropic.APIStatusError as e:
            logger.warning(f"Claude API エラー（{e.status_code}）: {e}")
            return None
        except self.anthropic.APIConnectionError as e:
            logger.warning(f"Claude API に接続できません: {e}")
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            logger.warning(f"[{d.code}] Claude の採点が完了しませんでした（{response.stop_reason}）")
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"[{d.code}] Claude の応答を JSON として読めません")
            return None
        return data["category"], max(-3.0, min(3.0, float(data["score"]))), data["reason"]


def score_disclosure(d: Disclosure, llm: Optional[ClaudeScorer] = None) -> Disclosure:
    d.category, d.score, d.reasons = rule_score(d.title, d.body)
    d.scored_by = "rule"
    # ルールで種類が分かり、点数が付かないもの（業績修正・決算など）や好材料候補だけ LLM に回して費用を抑える
    if llm and (d.score != 0 or d.category in ("業績予想の修正", "決算", "その他")):
        result = llm.score(d)
        if result:
            d.category, d.score = result[0], result[1]
            d.reasons = d.reasons + [f"Claude: {result[2]}"]
            d.scored_by = "claude"
    return d


# ---------------------------------------------------------------------------
# 取得元
# ---------------------------------------------------------------------------

class DisclosureSource:
    def fetch(self, day: date) -> List[Disclosure]:
        raise NotImplementedError


class CsvDisclosureSource(DisclosureSource):
    """folder/YYYY-MM-DD.csv（列: Code,Name,Time,Title[,Body,Url]）を読む"""

    def __init__(self, folder: str):
        self.folder = folder

    def fetch(self, day: date) -> List[Disclosure]:
        path = os.path.join(self.folder, f"{day.isoformat()}.csv")
        if not os.path.exists(path):
            return []
        result = []
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                hhmm = (row.get("Time") or "00:00")[:5]
                result.append(Disclosure(
                    code=normalize_code(row.get("Code")), name=row.get("Name", ""),
                    disclosed_at=f"{day.isoformat()}T{hhmm}:00", title=row.get("Title", ""),
                    body=row.get("Body", ""), url=row.get("Url", ""),
                ))
        return result


class JQuantsTdnetSource(DisclosureSource):
    """
    J-Quants API の TDnet アドオンから1日分の開示一覧を取る。
    仕様（URL・認証ヘッダ・項目名）は契約後に公式の仕様書で確認し、環境変数で合わせること。
      DS_JQ_URL          一覧のエンドポイント（必須）
      DS_JQ_API_KEY      API キー（または ID トークン）
      DS_JQ_AUTH_HEADER  認証ヘッダ名（既定 Authorization。値は "Bearer <キー>"）
      DS_JQ_AUTH_PREFIX  ヘッダ値の接頭辞（既定 "Bearer "。x-api-key 形式なら空にする）
      DS_JQ_DATE_PARAM   日付のクエリ名（既定 date）
      DS_JQ_LIST_KEY     一覧が入っている JSON のキー（既定 data）
      DS_JQ_PAGE_KEY     次ページのキー（既定 pagination_key。無ければ1ページだけ）
      DS_JQ_FIELDS       項目名の対応 "code=Code,name=CompanyName,date=Date,time=Time,title=Title,url=Url"
    """

    def __init__(self, url: str, api_key: str, auth_header: str = "Authorization",
                 auth_prefix: str = "Bearer ", date_param: str = "date", list_key: str = "data",
                 page_key: str = "pagination_key", fields: Optional[Dict[str, str]] = None):
        self.url = url
        self.api_key = api_key
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.date_param = date_param
        self.list_key = list_key
        self.page_key = page_key
        self.fields = {"code": "Code", "name": "CompanyName", "date": "Date", "time": "Time",
                       "title": "Title", "url": "Url", "body": "Body"}
        self.fields.update(fields or {})

    @classmethod
    def from_env(cls) -> "JQuantsTdnetSource":
        url = os.getenv("DS_JQ_URL")
        if not url:
            raise SystemExit("Error: DS_JQ_URL（J-Quants TDnet アドオンの一覧エンドポイント）を設定してください")
        fields = {}
        for pair in os.getenv("DS_JQ_FIELDS", "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                fields[k.strip()] = v.strip()
        return cls(url, os.getenv("DS_JQ_API_KEY", ""),
                   os.getenv("DS_JQ_AUTH_HEADER", "Authorization"),
                   os.getenv("DS_JQ_AUTH_PREFIX", "Bearer "),
                   os.getenv("DS_JQ_DATE_PARAM", "date"), os.getenv("DS_JQ_LIST_KEY", "data"),
                   os.getenv("DS_JQ_PAGE_KEY", "pagination_key"), fields)

    def _get(self, params: dict) -> dict:
        req = urllib.request.Request(f"{self.url}?{urllib.parse.urlencode(params)}", method="GET")
        if self.api_key:
            req.add_header(self.auth_header, f"{self.auth_prefix}{self.api_key}")
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())

    def fetch(self, day: date) -> List[Disclosure]:
        f = self.fields
        params = {self.date_param: day.isoformat()}
        result: List[Disclosure] = []
        while True:
            try:
                data = self._get(params)
            except Exception as e:
                logger.error(f"J-Quants TDnet の取得に失敗しました: {e}")
                return result
            for row in data.get(self.list_key) or []:
                t = str(row.get(f["time"]) or "00:00:00")
                t = t if len(t) >= 8 else (t + ":00")[:8]
                d = str(row.get(f["date"]) or day.isoformat())[:10]
                result.append(Disclosure(
                    code=normalize_code(row.get(f["code"])), name=str(row.get(f["name"]) or ""),
                    disclosed_at=f"{d}T{t[:8]}", title=str(row.get(f["title"]) or ""),
                    body=str(row.get(f["body"]) or ""), url=str(row.get(f["url"]) or ""),
                ))
            next_key = data.get(self.page_key)
            if not next_key:
                return result
            params[self.page_key] = next_key


def source_from_env() -> Optional[DisclosureSource]:
    """DS_SOURCE=jquants / csv / none（既定 csv: DS_CSV_DIR のフォルダ）"""
    kind = os.getenv("DS_SOURCE", "csv").strip().lower()
    if kind == "jquants":
        return JQuantsTdnetSource.from_env()
    if kind == "csv":
        return CsvDisclosureSource(os.getenv("DS_CSV_DIR", os.path.join(KABUCOM_DIR, "disclosures_csv")))
    return None


def scorer_from_env() -> Optional[ClaudeScorer]:
    if os.getenv("DS_LLM_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    return ClaudeScorer(model=os.getenv("DS_LLM_MODEL", "claude-opus-5-5"),
                        effort=os.getenv("DS_LLM_EFFORT", "low"))


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

class DisclosureStore:
    def __init__(self, folder: str = DEFAULT_STORE_DIR):
        self.folder = folder

    def _path(self, day: date) -> str:
        return os.path.join(self.folder, f"{day.isoformat()}.json")

    def has_day(self, day: date) -> bool:
        return os.path.exists(self._path(day))

    def load_day(self, day: date) -> List[Disclosure]:
        try:
            with open(self._path(day), encoding="utf-8") as f:
                return [Disclosure(**d) for d in json.load(f)]
        except FileNotFoundError:
            return []

    def save_day(self, day: date, items: List[Disclosure]):
        os.makedirs(self.folder, exist_ok=True)
        tmp = self._path(day) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in items], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path(day))

    def update(self, day: date, source: DisclosureSource, llm: Optional[ClaudeScorer] = None) -> List[Disclosure]:
        """その日の開示を取り直し、まだ採点していないものだけ採点して保存する"""
        known = {(d.code, d.disclosed_at, d.title): d for d in self.load_day(day)}
        merged = []
        new = 0
        for d in source.fetch(day):
            key = (d.code, d.disclosed_at, d.title)
            if key in known:
                merged.append(known.pop(key))
            else:
                merged.append(score_disclosure(d, llm))
                new += 1
        merged.extend(known.values())  # 取得元から消えたものも残す
        merged.sort(key=lambda d: d.disclosed_at)
        self.save_day(day, merged)
        if new:
            logger.info(f"{day} の開示を {new} 件追加しました（合計 {len(merged)} 件）")
        return merged

    def between(self, start: datetime, end: datetime) -> List[Disclosure]:
        """start 以上 end 未満の開示"""
        result = []
        d = start.date()
        while d <= end.date():
            result.extend(x for x in self.load_day(d) if start <= x.time < end)
            d += timedelta(days=1)
        return result
