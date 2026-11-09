"""AI 단타 대시보드 (로컬 웹 UI).

파이썬 표준 라이브러리(http.server)만으로 동작하며 127.0.0.1 전용이다.
AI(Claude Fable)가 거래량·등락률 상위에서 매매 종목을 자동 선정하고,
목표 수익(+1~2%) 익절 / -3% 손절을 반복하는 단타 루프를 제어한다.
실행: python -m autotrader ui
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .analysis import ClaudeAnalyst, ClaudeCodeAnalyst
from .kiwoom_rest import (
    KiwoomRestClient, KiwoomRestError, _read_parser, load_config,
)
from .scalper import ScalpConfig, Scalper


class RecordingAPI:
    """MarketAPI 래퍼: 시세 이력을 차트용으로 기록한다."""

    def __init__(self, inner):
        self.inner = inner
        self.prices: list[tuple[str, str, float]] = []   # (시각, 종목, 가격)
        self.lock = threading.Lock()

    def current_price(self, code: str) -> float:
        price = self.inner.current_price(code)
        with self.lock:
            self.prices.append((datetime.now().strftime("%H:%M:%S"), code, price))
            del self.prices[:-1800]
        return price

    def __getattr__(self, name):
        # send_market_order, top_stocks, us_balances 등은 원본으로 위임
        return getattr(self.inner, name)


def _parse_float(value, name: str) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 값이 숫자가 아니다: {value!r}")


def _parse_int(value, name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 값이 정수가 아니다: {value!r}")


class AppState:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.trader: Scalper | None = None
        self.api: RecordingAPI | None = None
        self.thread: threading.Thread | None = None
        self.params: dict = {}
        self.last_error = ""
        self.lock = threading.Lock()
        # 테스트에서 가짜 클라이언트/분석기를 주입할 수 있게 팩토리로 분리
        self.client_factory = KiwoomRestClient
        self.analyst_factory = ClaudeAnalyst          # API 키 방식
        self.cli_analyst_factory = ClaudeCodeAnalyst  # Claude Code 구독 로그인

    def _make_analyst(self):
        """분석 백엔드 선택: api_key가 있으면 API, 없으면 Claude Code 로그인.

        config.ini [claude] backend = auto(기본) | api | cli 로 강제할 수 있다.
        """
        parser = _read_parser(self.config_path)
        api_key, backend = "", "auto"
        if parser.has_section("claude"):
            section = parser["claude"]
            api_key = section.get("api_key", "").strip()
            backend = (section.get("backend", "auto").strip().lower() or "auto")
        if backend == "api" or (backend == "auto" and api_key):
            return self.analyst_factory(api_key)
        return self.cli_analyst_factory()

    # ── 실행 제어 ──────────────────────────────────────────
    def start(self, market: str, target_pct, max_positions, cash,
              confirm: str = "", stop_pct=1.0, require_ai: bool = True) -> dict:
        with self.lock:
            if self.trader is not None and self.thread and self.thread.is_alive():
                return {"error": "이미 실행 중이다. 먼저 중지해야 한다."}
            cfg = load_config(self.config_path)
            mock = cfg["mode"] != "real"
            if not mock and confirm != "YES":
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 확인란에 YES를 입력해야 시작된다."}
            try:
                scalp_cfg = ScalpConfig(
                    market=market,
                    target_pct=_parse_float(target_pct, "목표 수익 %"),
                    stop_pct=_parse_float(stop_pct, "손절 %"),
                    require_ai=bool(require_ai),
                    max_positions=_parse_int(max_positions, "동시 종목 수"),
                    total_cash=_parse_float(cash, "총 투입 금액"),
                    allow_real=not mock,
                    us_day_session=cfg.get("us_day_session", True),
                )
                client = self.client_factory(cfg["appkey"], cfg["secretkey"],
                                             mock=mock, market=market)
                api = RecordingAPI(client)

                def selector(rows, num, _self=self):
                    # 선정 시점에 분석 백엔드를 만든다. 백엔드가 없으면
                    # 예외가 나고, Scalper가 모멘텀 상위로 자동 대체한다.
                    analyst = _self._make_analyst()
                    return analyst.select_symbols(
                        market, rows, num, criteria_label="당일 거래량·등락률 상위")

                # 프로그램이 산 포지션을 재시작 후에도 이어서 관리하기 위한 상태 파일
                state_path = str(Path(self.config_path).resolve()
                                 .with_name(f"autotrader_state_{market}.json"))
                trader = Scalper(api=api, config=scalp_cfg, selector=selector,
                                 is_simulation=mock, state_path=state_path)
            except (ValueError, PermissionError) as e:
                return {"error": str(e)}

            def run_guarded():
                try:
                    trader.run()
                except Exception as e:  # 스레드가 죽어도 UI에 이유를 남긴다
                    self.last_error = f"매매 스레드 종료: {e}"

            thread = threading.Thread(target=run_guarded, daemon=True)
            self.trader, self.api, self.thread = trader, api, thread
            self.params = {"market": market,
                           "target_pct": scalp_cfg.target_pct,
                           "stop_pct": scalp_cfg.stop_pct,
                           "require_ai": scalp_cfg.require_ai,
                           "max_positions": scalp_cfg.max_positions,
                           "cash": scalp_cfg.total_cash,
                           "mode": "mock" if mock else "real"}
            self.last_error = ""
            thread.start()
            return {"ok": True}

    def stop(self) -> dict:
        with self.lock:
            if self.trader is None:
                return {"error": "실행 중이 아니다."}
            self.trader.stop()
            return {"ok": True}

    def adopt(self, code: str, mode: str) -> dict:
        """계좌 보유 종목을 관리 대상으로 편입(manage)하거나 즉시 매도(sell)한다."""
        if not (self.trader and self.thread and self.thread.is_alive()):
            return {"error": "먼저 시작해야 한다 (장중에 처리된다)."}
        if mode not in ("manage", "sell"):
            return {"error": f"알 수 없는 요청: {mode}"}
        code = (code or "").strip().upper()
        if not code:
            return {"error": "종목코드가 비어 있다."}
        self.trader.request(code, mode)
        return {"ok": True}

    # ── 상태 조회 ──────────────────────────────────────────
    def status(self) -> dict:
        mode = "mock"
        try:
            mode = load_config(self.config_path)["mode"]
        except KiwoomRestError:
            pass
        running = bool(self.trader and self.thread and self.thread.is_alive())
        out = {
            "running": running,
            "config_mode": mode,
            "params": self.params,
            "last_error": self.last_error
                          or getattr(self.trader, "last_error", ""),
            "prices": [], "events": [], "positions": [], "trades": [],
            "equity": None, "cash": None, "realized_pnl": None,
            "wins": 0, "losses": 0, "session": "", "halt_reason": "",
            "selection_note": "", "chart_code": "", "holdings": [],
        }
        trader = self.trader
        if trader:
            out["equity"] = trader.equity()
            out["cash"] = trader.cash
            out["realized_pnl"] = trader.realized_pnl
            out["wins"] = trader.wins
            out["losses"] = trader.losses
            out["session"] = trader.session()
            left = trader.minutes_to_close()
            out["minutes_to_close"] = None if left is None else round(left)
            out["halt_reason"] = trader.halt_reason
            out["selection_note"] = trader.selection_note
            out["events"] = trader.events[-30:]
            now = trader.clock()
            out["positions"] = [
                {"code": p.code, "qty": p.quantity,
                 "entry": p.entry_price, "last": p.last_price,
                 "pct": round(p.pct(), 2),
                 "held_min": round((now - p.entry_time).total_seconds() / 60),
                 "state": p.state()}
                for p in list(trader.positions.values())
            ]
            managed = set(trader.positions)
            out["holdings"] = [
                {"code": code, "qty": h.get("qty", 0),
                 "sellable": h.get("sellable", 0),
                 "avg": h.get("avg_price", 0), "now": h.get("now_price", 0),
                 "pl_rt": h.get("pl_rt", 0), "managed": code in managed}
                for code, h in sorted(trader.holdings.items())
                if h.get("qty", 0) > 0
            ]
            out["trades"] = [
                f"{t.closed_at.strftime('%H:%M:%S')} {t.reason} {t.code} "
                f"{t.quantity}주 {t.pct:+.2f}% ({t.pnl:+,.0f})"
                for t in trader.trades[-20:]
            ]
            codes = list(trader.positions)
            chart_code = codes[0] if codes else ""
            out["chart_code"] = chart_code
            if self.api and chart_code:
                with self.api.lock:
                    out["prices"] = [(t, p) for t, c, p in self.api.prices
                                     if c == chart_code][-300:]
        return out


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoTrader AI 단타</title>
<style>
:root{--bg:#12151c;--card:#1b2029;--line:#2b3342;--txt:#e6e9ef;--dim:#8b93a3;
--up:#e5484d;--down:#3b82f6;--acc:#f0b429;--ok:#22c55e}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 'Malgun Gothic',sans-serif;
padding:20px;max-width:1080px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px} .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card .k{color:var(--dim);font-size:12px} .card .v{font-size:18px;font-weight:700;margin-top:2px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.row label{color:var(--dim);font-size:12px}
select,input,button{background:#232937;color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:8px 12px;font-size:14px}
button{cursor:pointer} button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);color:#1a1a1a;font-weight:700;border:none}
button.danger{background:#7f1d1d;border:none}
#chartWrap{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px} canvas{width:100%;height:200px;display:block}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:110px;margin-bottom:14px}
.panel h2{font-size:14px;color:var(--dim);margin-bottom:8px}
ul{list-style:none} li{padding:3px 0;border-bottom:1px solid var(--line);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}
th{color:var(--dim);font-weight:400} td:first-child,th:first-child{text-align:left}
.pos{color:var(--ok)} .neg{color:#fca5a5}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.mock{background:#14532d;color:#86efac} .badge.real{background:#7f1d1d;color:#fecaca}
#realConfirm{display:none} .warn{color:#fca5a5;font-size:12px;margin:6px 0}
#note{white-space:pre-wrap;font-size:13px}
.fixed{color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px}
</style></head><body>
<h1>AutoTrader AI 단타 <span id="modeBadge" class="badge mock">모의투자</span></h1>
<div class="sub">AI가 종목을 골라 매수 → 좁은 손절 / 목표 도달 후 고점을 따라가며 익절 · 장 시작 15분 뒤~마감 1시간 전</div>

<div class="row">
  <select id="market"><option value="kr">국내주식</option><option value="us">미국주식</option></select>
  <span><label>목표 수익 %</label><br><input id="target" value="2" size="4" title="이 수익률에 닿으면 바로 팔지 않고, 고점에서 0.7% 밀릴 때 익절합니다"></span>
  <span><label>손절 %</label><br><input id="stop" value="1" size="4" title="매수가 대비 이만큼 내리면 무조건 전량 매도. 목표 수익보다 작게 두세요"></span>
  <label title="켜면 AI 선정이 안 될 때 등락률 상위로 대신 사지 않고 매수를 쉽니다"><input type="checkbox" id="needAI" checked> AI 선정 필수</label>
  <span><label>동시 종목</label><br><input id="maxpos" value="2" size="3"></span>
  <span><label>총 투입 금액</label><br><input id="cash" value="1000000" size="10"></span>
  <span id="realConfirm"><label>실전 확인</label><br><input id="confirm" placeholder="YES 입력" size="6"></span>
  <button class="primary" onclick="start()">시작</button>
  <button class="danger" onclick="stopT()">중지</button>
</div>
<div class="warn" id="msg"></div>

<div class="grid">
  <div class="card"><div class="k">상태</div><div class="v" id="stRun">대기</div></div>
  <div class="card"><div class="k">평가액</div><div class="v" id="stEquity">-</div></div>
  <div class="card"><div class="k">실현손익</div><div class="v" id="stPnl">-</div></div>
  <div class="card"><div class="k">오늘 매매</div><div class="v" id="stTrades">-</div></div>
  <div class="card"><div class="k">승 / 패</div><div class="v" id="stWinLoss">-</div></div>
  <div class="card"><div class="k">현금</div><div class="v" id="stCash">-</div></div>
</div>

<div class="panel"><h2>보유 포지션</h2>
  <table><thead><tr><th>종목</th><th>수량</th><th>진입가</th><th>현재가</th>
  <th>수익률</th><th>보유(분)</th><th>상태</th></tr></thead>
  <tbody id="positions"><tr><td colspan="7">없음</td></tr></tbody></table>
</div>

<div class="panel"><h2>실계좌 보유 종목 <span class="fixed">(1분마다 동기화 · 프로그램 밖의 보유는 손대지 않습니다)</span></h2>
  <table><thead><tr><th>종목</th><th>보유</th><th>매입단가</th><th>현재가</th>
  <th>손익률</th><th>처리</th></tr></thead>
  <tbody id="holdings"><tr><td colspan="6">시작 후 1분 안에 표시됩니다 (미국주식)</td></tr></tbody></table>
</div>

<div id="chartWrap"><canvas id="chart" width="1040" height="200"></canvas></div>

<div class="cols">
  <div class="panel"><h2>청산 기록 / 이벤트</h2><ul id="log"><li>기록 없음</li></ul></div>
  <div class="panel"><h2>Fable 종목 선정 근거</h2><div id="note">아직 선정 전입니다.
AI 선정이 불가하면 등락률 모멘텀 상위로 자동 대체됩니다.
선정은 참고 자료이며 투자 자문이 아닙니다.</div></div>
</div>

<script>
const $=id=>document.getElementById(id);
$("market").onchange=()=>{ $("cash").value = $("market").value==="us" ? "1000" : "1000000"; };
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",
headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
return r.json();}
async function start(){
  const r=await api("/api/start",{market:$("market").value,
    target_pct:$("target").value.trim(),stop_pct:$("stop").value.trim(),
    require_ai:$("needAI").checked,max_positions:$("maxpos").value.trim(),
    cash:$("cash").value.trim(),confirm:$("confirm").value.trim()});
  $("msg").textContent=r.error||"AI 단타 시작 - 장중에 종목을 자동 선정해 매매합니다";
}
async function adopt(code,mode){
  const msg=mode==="sell"?code+" 보유 수량 전부를 지금 매도합니다. 진행할까요?"
    :code+"을(를) 관리 대상으로 편입합니다. 익절 +목표%·손절 -3% 규칙이 즉시 적용되어, 이미 -3% 아래면 바로 손절 매도됩니다. 진행할까요?";
  if(!confirm(msg))return;
  const r=await api("/api/adopt",{code:code,mode:mode});
  $("msg").textContent=r.error||(code+" 요청 접수 — 다음 주기(3초 내)에 처리됩니다");
}
async function stopT(){const r=await api("/api/stop",{});$("msg").textContent=r.error||"중지 요청 완료";}
function drawChart(prices,code){
  const c=$("chart"),x=c.getContext("2d");x.clearRect(0,0,c.width,c.height);
  if(prices.length<2){x.fillStyle="#8b93a3";x.fillText("보유 포지션이 생기면 해당 종목 시세가 표시됩니다",20,40);return;}
  const vals=prices.map(p=>p[1]),min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)||1;
  x.strokeStyle="#f0b429";x.lineWidth=1.6;x.beginPath();
  prices.forEach((p,i)=>{const px=30+(c.width-50)*i/(prices.length-1),
    py=15+(c.height-40)*(1-(p[1]-min)/pad);i?x.lineTo(px,py):x.moveTo(px,py);});
  x.stroke();
  x.fillStyle="#8b93a3";x.fillText((code?code+"  ":"")+max.toLocaleString(),4,18);
  x.fillText(min.toLocaleString(),4,c.height-22);
}
async function refresh(){
  try{
    const s=await api("/api/status");
    const real=s.config_mode==="real";
    $("modeBadge").textContent=real?"실전투자":"모의투자";
    $("modeBadge").className="badge "+(real?"real":"mock");
    $("realConfirm").style.display=real?"inline-block":"none";
    let run=s.running?("실행 중 · "+(s.session||"-")):"대기";
    if(s.running&&s.session==="장외")run="대기 (장외)";
    if(s.running&&s.minutes_to_close!=null)run+=" · 마감까지 "+s.minutes_to_close+"분";
    if(s.halt_reason)run+=" · "+s.halt_reason;
    $("stRun").textContent=run;
    $("stEquity").textContent=s.equity!=null?Math.round(s.equity).toLocaleString():"-";
    const pnl=s.realized_pnl;
    $("stPnl").textContent=pnl!=null?Math.round(pnl).toLocaleString():"-";
    $("stPnl").className="v "+(pnl>0?"pos":(pnl<0?"neg":""));
    $("stTrades").textContent=(s.wins+s.losses)+"회";
    $("stWinLoss").textContent=s.wins+" / "+s.losses;
    $("stCash").textContent=s.cash!=null?Math.round(s.cash).toLocaleString():"-";
    $("positions").innerHTML=(s.positions&&s.positions.length)
      ?s.positions.map(p=>"<tr><td>"+p.code+"</td><td>"+p.qty+"</td><td>"+
        p.entry.toLocaleString(undefined,{maximumFractionDigits:2})+"</td><td>"+
        (p.last?p.last.toLocaleString(undefined,{maximumFractionDigits:2}):"-")+
        "</td><td class='"+(p.pct>=0?"pos":"neg")+"'>"+p.pct.toFixed(2)+"%</td><td>"+
        p.held_min+"</td><td>"+(p.state||"")+"</td></tr>").join("")
      :"<tr><td colspan='7'>없음</td></tr>";
    if(s.holdings&&s.holdings.length){
      $("holdings").innerHTML=s.holdings.map(h=>"<tr><td>"+h.code+"</td><td>"+h.qty+
        "</td><td>"+Number(h.avg).toLocaleString(undefined,{maximumFractionDigits:2})+
        "</td><td>"+Number(h.now).toLocaleString(undefined,{maximumFractionDigits:2})+
        "</td><td class='"+(h.pl_rt>=0?"pos":"neg")+"'>"+Number(h.pl_rt).toFixed(2)+
        "%</td><td>"+(h.managed?"관리 중":
        "<button data-c='"+h.code+"' data-m='manage' onclick='adopt(this.dataset.c,this.dataset.m)'>관리 편입</button> "+
        "<button class='danger' data-c='"+h.code+"' data-m='sell' onclick='adopt(this.dataset.c,this.dataset.m)'>즉시 매도</button>")+
        "</td></tr>").join("");
    }
    drawChart(s.prices,s.chart_code);
    $("log").innerHTML=[...s.trades,...s.events].slice(-15).reverse()
      .map(e=>"<li>"+e+"</li>").join("")||"<li>기록 없음</li>";
    if(s.selection_note)$("note").textContent="[Fable 선정 근거]\\n"+s.selection_note+
      "\\n\\n※ 참고 자료이며 투자 자문이 아닙니다.";
    if(s.last_error)$("msg").textContent=s.last_error;
  }catch(e){}
}
setInterval(refresh,2000);refresh();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    state: AppState  # run_ui에서 주입

    def log_message(self, *args):  # 콘솔 소음 억제
        pass

    def _send(self, obj, code=200, content_type="application/json"):
        data = (obj if isinstance(obj, bytes)
                else json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE.encode("utf-8"), content_type="text/html")
        elif self.path == "/api/status":
            self._send(self.state.status())
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        try:
            if self.path == "/api/start":
                self._send(self.state.start(
                    market=body.get("market", "kr"),
                    target_pct=body.get("target_pct", 2.0),
                    stop_pct=body.get("stop_pct", 1.0),
                    require_ai=bool(body.get("require_ai", True)),
                    max_positions=body.get("max_positions", 2),
                    cash=body.get("cash"),
                    confirm=body.get("confirm", ""),
                ))
            elif self.path == "/api/stop":
                self._send(self.state.stop())
            elif self.path == "/api/adopt":
                self._send(self.state.adopt(body.get("code", ""),
                                            body.get("mode", "")))
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:  # 어떤 오류든 콘솔 스택 대신 화면에 안내한다
            self._send({"error": f"{type(e).__name__}: {e}"})


def run_ui(config_path: str = "config.ini", port: int = 8899,
           open_browser: bool = True) -> None:
    state = AppState(config_path)
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"AutoTrader AI 단타 대시보드: {url}  (종료: Ctrl+C)")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.trader:
            state.trader.stop()
        server.server_close()
