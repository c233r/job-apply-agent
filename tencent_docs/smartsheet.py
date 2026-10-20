# -*- coding: utf-8 -*-
"""腾讯文档个人版「智能表」抓取模块。

无需登录即可读取公开分享的智能表。原理：
1. 访问 https://docs.qq.com/smartsheet/<docid> 拿到 dop-api/opendoc 接口地址；
2. opendoc 返回 clientVars.collab_client_vars.initialAttributedText.text[]，
   其中 workbook 是子表列表，smartsheet 是 base64+zlib 压缩的表数据；
3. 解压后得到 mutation 列表：t=3005 为表结构（字段定义），t=3028 为记录数据。

依赖：requests（标准第三方库，见 requirements.txt）。
"""
import base64
import json
import re
import zlib

import requests

H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://docs.qq.com/",
}

OPEN_DOC = "https://docs.qq.com/dop-api/opendoc"


class SmartsheetError(Exception):
    pass


def _fetch_json(url: str, timeout: int = 60) -> dict:
    resp = requests.get(url, headers=H, timeout=timeout)
    resp.raise_for_status()
    text = resp.text.strip()
    # JSONP 包装：clientVarsCallback({...})
    if text.startswith("clientVarsCallback(") and text.endswith(")"):
        text = text[len("clientVarsCallback("):-1]
    return json.loads(text)


def _decode_blob(b64: str) -> list:
    """解压 smartsheet 字段（base64 -> zlib -> JSON）。"""
    b64 = b64.strip()
    b64 += "=" * (-len(b64) % 4)  # 补 base64 填充
    raw = base64.b64decode(b64)
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            return json.loads(zlib.decompress(raw, wbits).decode("utf-8"))
        except (zlib.error, UnicodeDecodeError):
            continue
    raise SmartsheetError("无法解压 smartsheet 数据")


class SmartsheetDoc:
    """一个智能表文档（包含多个子表）。"""

    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._sheets = None

    # ---- 子表 ----
    @property
    def sheets(self) -> list:
        if self._sheets is None:
            self._sheets = self._list_sheets()
        return self._sheets

    def _list_sheets(self) -> list:
        j = _fetch_json(self._opendoc_url())
        texts = self._texts(j)
        if not texts:
            raise SmartsheetError("opendoc 未返回 workbook 数据")
        workbook = json.loads(texts[0].get("workbook", "[]"))
        return [w for w in workbook if w.get("type") == "smartsheet"]

    def _opendoc_url(self, tab: str | None = None, startrow: int = 0, endrow: int = 10000) -> str:
        qs = [
            "id=" + self.doc_id,
            "normal=1", "outformat=1", "wb=1", "nowb=0",
            "startrow=%d" % startrow, "endrow=%d" % endrow,
            "enableSmartsheetSplit=1", "supportOptimizedVer=4",
            "chunkCellSize=15000", "enableChunkRank=1",
            "callback=clientVarsCallback", "noEscape=1",
        ]
        if tab:
            qs.insert(0, "tab=" + tab)
        return OPEN_DOC + "?" + "&".join(qs)

    @staticmethod
    def _texts(j: dict) -> list:
        return (
            j.get("clientVars", {})
            .get("collab_client_vars", {})
            .get("initialAttributedText", {})
            .get("text") or []
        )

    # ---- 单表读取 ----
    def read_sheet(self, sheet_id: str) -> "SheetData":
        j = _fetch_json(self._opendoc_url(tab=sheet_id))
        texts = self._texts(j)
        schema, records = {}, {}
        for t in texts:
            blob = _decode_blob(t.get("smartsheet", ""))
            for seg in (blob if isinstance(blob, list) else []):
                for m in (seg if isinstance(seg, list) else []):
                    if not isinstance(m, dict):
                        continue
                    if m.get("t") == 3005:
                        table = m.get("c", {}).get("k3", {})
                        schema = table.get("k3", {}) if isinstance(table, dict) else {}
                    elif m.get("t") == 3028:
                        k2 = m.get("c", {}).get("k2", {})
                        records = k2.get("k1", {}) if isinstance(k2, dict) else {}
        return SheetData(schema, records)


class SheetData:
    """一个子表的字段 + 记录，可导出为列表/CSV。"""

    def __init__(self, schema: dict, records: dict):
        self.schema = schema
        self.records = records

    @property
    def fields(self) -> list:
        """[(field_id, title, type)] 按列顺序返回。type: 1=文本, 3=单选, 4=多选 ..."""
        out = []
        for fid, fdef in self.schema.items():
            if not isinstance(fdef, dict):
                continue
            title = fdef.get("k30", fid)
            ftype = fdef.get("k31", 1)
            out.append((fid, title, ftype))
        return out

    def _field_value_text(self, fdef: dict) -> str:
        """把一个字段值对象转成可读文本。

        文本片段结构：{"k1": 片段类型, "k2": 文本, "k3": 链接(若有)}
        片段类型 "url" 时 k3 是真实地址；"text" 时 k2 是纯文本。
        """
        if not isinstance(fdef, dict):
            return ""
        k1 = fdef.get("k1")
        if isinstance(k1, list):
            parts = []
            for seg in k1:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("k1")
                link = seg.get("k3")
                if seg_type == "url" and isinstance(link, str) and link.startswith("http"):
                    parts.append(link)  # 链接字段取真实地址
                else:
                    text = seg.get("k2")
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        if isinstance(k1, str):
            return k1
        return ""

    def row_dicts(self) -> list:
        """返回 [{"列名": "值", ...}, ...]，额外带 _record_id。"""
        headers = [title for _, title, _ in self.fields]
        rows = []
        for rid, rec in self.records.items():
            fv = rec.get("k1", {}) if isinstance(rec, dict) else {}
            row = {"_record_id": rid}
            for fid, title, _ in self.fields:
                row[title] = self._field_value_text(fv.get(fid))
            rows.append(row)
        return rows

    def to_csv(self) -> str:
        import csv
        import io

        headers = [title for _, title, _ in self.fields]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        for row in self.row_dicts():
            w.writerow([row.get(h, "") for h in headers])
        return buf.getvalue()


def extract_docid(url: str) -> str:
    """从分享链接提取 docid。"""
    m = re.search(r"docs\.qq\.com/smartsheet/([A-Za-z0-9]+)", url)
    if not m:
        raise SmartsheetError("无法从链接提取 docid: %s" % url)
    return m.group(1)


def fetch_doc(url: str) -> SmartsheetDoc:
    return SmartsheetDoc(extract_docid(url))
