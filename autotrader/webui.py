"""로컬 웹 대시보드 UI.

파이썬 표준 라이브러리(http.server)만으로 동작하는 브라우저 UI다.
127.0.0.1 전용으로 바인딩하며, 시작/중지·시세 차트·손익 현황·백테스트·
Claude(Fable) AI 분석을 제공한다. 실행: python -m autotrader ui
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .analysis import ClaudeAnalyst, MarketSnapshot
from .broker import PaperBroker
from .data import generate_synthetic_bars
from .engine import run_backtest
from .kiwoom.live import LiveConfig, LiveTrader
from .kiwoom_rest import (
    KiwoomRestClient, KiwoomRestError, _read_parser, load_config, load_risk_config,
)
from .risk import RiskConfig, RiskManager
from .strategy import STRATEGIES


class RecordingAPI:
    """MarketAPI 래퍼: 시세·주문 이력을 UI용으로 기록한다."""

    def __init__(self, inner):
        self.inner = inner
        self.prices: list[tuple[str, float]] = []
        self.events: list[str] = []
        self.lock = threading.Lock()

    def current_price(self, code: str) -> float:
        price = self.inner.current_price(code)
        with self.lock:
            self.prices.append((datetime.now().strftime("%H:%M:%S"), price))
            del self.prices[:-600]
        return price

    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None:
        self.inner.send_market_order(account_no, code, side, quantity)
        with self.lock:
            label = "매수" if side == "buy" else "매도"
            self.events.append(
                f"{datetime.now().strftime('%H:%M:%S')} {label} {code} {quantity}주")
            del self.events[:-100]


class AppState:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.trader: LiveTrader | None = None
        self.api: RecordingAPI | None = None
        self.thread: threading.Thread | None = None
        self.params: dict = {}
        self.last_error = ""
        self.lock = threading.Lock()

    # ── 실행 제어 ──────────────────────────────────────────
    def start(self, market: str, code: str, strategy: str, confirm: str) -> dict:
        with self.lock:
            if self.trader is not None and self.thread and self.thread.is_alive():
                return {"error": "이미 실행 중이다. 먼저 중지해야 한다."}
            cfg = load_config(self.config_path)
            mock = cfg["mode"] != "real"
            if not mock and confirm != "YES":
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 확인란에 YES를 입력해야 시작된다."}
            risk_cfg = load_risk_config(self.config_path) or RiskConfig()
            cash = 10_000 if market == "us" else 10_000_000
            client = KiwoomRestClient(cfg["appkey"], cfg["secretkey"], mock=mock,
                                      market=market)
            api = RecordingAPI(client)
            trader = LiveTrader(
                api=api,
                strategy=STRATEGIES[strategy](),
                config=LiveConfig(account_no="", code=code, market=market,
                                  initial_cash=cash, poll_interval=3.0,
                                  allow_real=not mock),
                risk=RiskManager(risk_cfg, initial_equity=cash),
                is_simulation=mock,
            )
            thread = threading.Thread(target=trader.run, daemon=True)
            self.trader, self.api, self.thread = trader, api, thread
            self.params = {"market": market, "code": code, "strategy": strategy,
                           "mode": "mock" if mock else "real", "cash": cash}
            self.last_error = ""
            thread.start()
            return {"ok": True}

    def stop(self) -> dict:
        with self.lock:
            if self.trader is None:
                return {"error": "실행 중이 아니다."}
            self.trader.stop()
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
            "last_error": self.last_error,
            "prices": [], "events": [], "fills": [],
            "equity": None, "cash": None, "realized_pnl": None,
            "position_qty": 0, "orders_today": 0, "bars_seen": 0,
        }
        if self.api:
            with self.api.lock:
                out["prices"] = self.api.prices[-300:]
                out["events"] = self.api.events[-30:]
        if self.trader:
            engine = self.trader.engine
            out["equity"] = engine.equity()
            out["cash"] = engine.account.cash
            out["realized_pnl"] = engine.account.realized_pnl
            pos = engine.account.positions.get(self.params.get("code", ""))
            out["position_qty"] = pos.quantity if pos else 0
            out["orders_today"] = self.trader.orders_today
            closes = getattr(engine.strategy, "_closes", None)
            out["bars_seen"] = len(closes) if closes is not None else 0
            out["fills"] = [
                f"{f.timestamp.strftime('%H:%M')} {'매수' if f.side.value == 'buy' else '매도'} "
                f"{f.quantity}주 @{f.price:,.2f}"
                for f in engine.fills[-20:]
            ]
        return out

    # ── 백테스트 / AI 분석 ─────────────────────────────────
    def backtest(self, strategy: str) -> dict:
        bars = generate_synthetic_bars("TEST", days=250)
        risk = RiskManager(RiskConfig(), initial_equity=10_000_000)
        result = run_backtest(STRATEGIES[strategy](), PaperBroker(), risk,
                              bars, initial_cash=10_000_000)
        return {
            "strategy": strategy,
            "return_pct": round(result.total_return_pct, 2),
            "mdd_pct": round(result.max_drawdown_pct, 2),
            "trades": result.num_trades,
        }

    def analyze(self) -> dict:
        parser = _read_parser(self.config_path)
        api_key = ""
        if parser.has_section("claude"):
            api_key = parser["claude"].get("api_key", "").strip()
        status = self.status()
        params = self.params or {"market": "kr", "code": "-", "strategy": "-",
                                 "mode": status["config_mode"]}
        pos_avg = 0.0
        if self.trader:
            pos = self.trader.engine.account.positions.get(params.get("code", ""))
            pos_avg = pos.avg_price if pos else 0.0
        snapshot = MarketSnapshot(
            market=params.get("market", "kr"),
            code=params.get("code", "-"),
            strategy=params.get("strategy", "-"),
            mode=params.get("mode", status["config_mode"]),
            prices=status["prices"],
            equity=status["equity"] or 0.0,
            cash=status["cash"] or 0.0,
            realized_pnl=status["realized_pnl"] or 0.0,
            position_qty=status["position_qty"],
            position_avg_price=pos_avg,
            orders_today=status["orders_today"],
            recent_fills=status["fills"],
        )
        analyst = ClaudeAnalyst(api_key)
        return {"text": analyst.analyze(snapshot)}


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoTrader</title>
<style>
:root{--bg:#12151c;--card:#1b2029;--line:#2b3342;--txt:#e6e9ef;--dim:#8b93a3;
--up:#e5484d;--down:#3b82f6;--acc:#f0b429}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 'Malgun Gothic',sans-serif;
padding:20px;max-width:1080px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px} .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card .k{color:var(--dim);font-size:12px} .card .v{font-size:18px;font-weight:700;margin-top:2px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
select,input,button{background:#232937;color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:8px 12px;font-size:14px}
button{cursor:pointer} button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);color:#1a1a1a;font-weight:700;border:none}
button.danger{background:#7f1d1d;border:none}
#chartWrap{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px} canvas{width:100%;height:220px;display:block}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:120px}
.panel h2{font-size:14px;color:var(--dim);margin-bottom:8px}
ul{list-style:none} li{padding:3px 0;border-bottom:1px solid var(--line);font-size:13px}
#analysis{white-space:pre-wrap;font-size:13px}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.mock{background:#14532d;color:#86efac} .badge.real{background:#7f1d1d;color:#fecaca}
#realConfirm{display:none} .warn{color:#fca5a5;font-size:12px;margin:6px 0}
</style></head><body>
<h1>AutoTrader <span id="modeBadge" class="badge mock">모의투자</span></h1>
<div class="sub">키움 REST API 자동매매 대시보드 · 분석: Claude Fable</div>

<div class="row">
  <select id="market"><option value="us">미국주식</option><option value="kr">국내주식</option></select>
  <input id="code" value="AAPL" size="8">
  <select id="strategy"><option value="sma_crossover">이동평균 교차</option>
  <option value="rsi_reversion">RSI 평균회귀</option></select>
  <span id="realConfirm">실전 확인: <input id="confirm" placeholder="YES 입력" size="6"></span>
  <button class="primary" id="btnStart" onclick="start()">시작</button>
  <button class="danger" onclick="stopT()">중지</button>
  <button onclick="backtest()">백테스트</button>
  <button onclick="analyze()" id="btnAI">AI 분석 (Fable)</button>
</div>
<div class="warn" id="msg"></div>

<div class="grid">
  <div class="card"><div class="k">상태</div><div class="v" id="stRun">대기</div></div>
  <div class="card"><div class="k">현재가</div><div class="v" id="stPrice">-</div></div>
  <div class="card"><div class="k">평가액</div><div class="v" id="stEquity">-</div></div>
  <div class="card"><div class="k">실현손익</div><div class="v" id="stPnl">-</div></div>
  <div class="card"><div class="k">보유수량</div><div class="v" id="stPos">-</div></div>
  <div class="card"><div class="k">오늘 주문</div><div class="v" id="stOrders">-</div></div>
</div>

<div id="chartWrap"><canvas id="chart" width="1040" height="220"></canvas></div>

<div class="cols">
  <div class="panel"><h2>체결 / 이벤트</h2><ul id="log"></ul></div>
  <div class="panel"><h2>Claude Fable 분석</h2><div id="analysis">아직 분석을 실행하지 않았습니다.
분석은 참고 자료이며 투자 자문이 아닙니다.</div></div>
</div>

<script>
const $=id=>document.getElementById(id);
let lastStatus=null;
$("market").onchange=()=>{ $("code").value = $("market").value==="us" ? "AAPL" : "005930"; };
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",
headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
return r.json();}
async function start(){
  const r=await api("/api/start",{market:$("market").value,code:$("code").value.trim(),
    strategy:$("strategy").value,confirm:$("confirm").value.trim()});
  $("msg").textContent=r.error||"";
}
async function stopT(){const r=await api("/api/stop",{});$("msg").textContent=r.error||"중지 요청 완료";}
async function backtest(){
  $("analysis").textContent="백테스트 실행 중...";
  const r=await api("/api/backtest",{strategy:$("strategy").value});
  $("analysis").textContent=r.error||("[가상시세 250일 백테스트] 전략 "+r.strategy+
    "\\n수익률 "+r.return_pct+"%  ·  최대낙폭 "+r.mdd_pct+"%  ·  체결 "+r.trades+"회");
}
async function analyze(){
  $("btnAI").disabled=true;$("analysis").textContent="Claude Fable이 분석 중입니다...";
  try{const r=await api("/api/analyze",{});
    $("analysis").textContent=r.error||r.text;}finally{$("btnAI").disabled=false;}
}
function drawChart(prices){
  const c=$("chart"),x=c.getContext("2d");x.clearRect(0,0,c.width,c.height);
  if(prices.length<2){x.fillStyle="#8b93a3";x.fillText("시세 수집 대기 중 (장중에만 수집됩니다)",20,40);return;}
  const vals=prices.map(p=>p[1]),min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)||1;
  x.strokeStyle="#f0b429";x.lineWidth=1.6;x.beginPath();
  prices.forEach((p,i)=>{const px=30+(c.width-50)*i/(prices.length-1),
    py=15+(c.height-40)*(1-(p[1]-min)/pad);i?x.lineTo(px,py):x.moveTo(px,py);});
  x.stroke();
  x.fillStyle="#8b93a3";x.fillText(max.toLocaleString(),4,18);x.fillText(min.toLocaleString(),4,c.height-22);
}
async function refresh(){
  try{
    const s=await api("/api/status");lastStatus=s;
    const real=s.config_mode==="real";
    $("modeBadge").textContent=real?"실전투자":"모의투자";
    $("modeBadge").className="badge "+(real?"real":"mock");
    $("realConfirm").style.display=real?"inline":"none";
    $("stRun").textContent=s.running?"실행 중":"대기";
    const last=s.prices.length?s.prices[s.prices.length-1][1]:null;
    $("stPrice").textContent=last?last.toLocaleString():"-";
    $("stEquity").textContent=s.equity!=null?Math.round(s.equity).toLocaleString():"-";
    $("stPnl").textContent=s.realized_pnl!=null?Math.round(s.realized_pnl).toLocaleString():"-";
    $("stPos").textContent=s.position_qty+"주";
    $("stOrders").textContent=s.orders_today+"회";
    drawChart(s.prices);
    $("log").innerHTML=[...s.fills,...s.events].slice(-15).reverse()
      .map(e=>"<li>"+e+"</li>").join("")||"<li>기록 없음</li>";
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
                    code=(body.get("code") or "005930").strip().upper(),
                    strategy=body.get("strategy", "sma_crossover"),
                    confirm=body.get("confirm", ""),
                ))
            elif self.path == "/api/stop":
                self._send(self.state.stop())
            elif self.path == "/api/backtest":
                self._send(self.state.backtest(body.get("strategy", "sma_crossover")))
            elif self.path == "/api/analyze":
                self._send(self.state.analyze())
            else:
                self._send({"error": "not found"}, 404)
        except (KiwoomRestError, PermissionError, RuntimeError, KeyError) as e:
            self._send({"error": str(e)})


def run_ui(config_path: str = "config.ini", port: int = 8899,
           open_browser: bool = True) -> None:
    state = AppState(config_path)
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"AutoTrader 대시보드: {url}  (종료: Ctrl+C)")
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
