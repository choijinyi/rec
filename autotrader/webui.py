"""분할매매 대시보드 (로컬 웹 UI).

파이썬 표준 라이브러리(http.server)만으로 동작하며 127.0.0.1 전용이다.
종목·기준 가격·분할 %·분할 횟수·총 투입 금액을 입력하면 SplitTrader가
기준 가격에서부터 분할매수·분할매도를 자동으로 집행한다.
실행: python -m autotrader ui
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .kiwoom_rest import KiwoomRestClient, KiwoomRestError, load_config
from .split import SplitConfig, SplitTrader


class RecordingAPI:
    """MarketAPI 래퍼: 시세 이력을 차트용으로 기록한다."""

    def __init__(self, inner):
        self.inner = inner
        self.prices: list[tuple[str, float]] = []
        self.lock = threading.Lock()

    def current_price(self, code: str) -> float:
        price = self.inner.current_price(code)
        with self.lock:
            self.prices.append((datetime.now().strftime("%H:%M:%S"), price))
            del self.prices[:-1800]
        return price

    def __getattr__(self, name):
        # send_market_order, us_balances 등은 원본 클라이언트로 위임
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
        self.trader: SplitTrader | None = None
        self.api: RecordingAPI | None = None
        self.thread: threading.Thread | None = None
        self.params: dict = {}
        self.last_error = ""
        self.lock = threading.Lock()
        # 테스트에서 가짜 클라이언트를 주입할 수 있게 팩토리로 분리
        self.client_factory = KiwoomRestClient

    # ── 실행 제어 ──────────────────────────────────────────
    def start(self, market: str, code: str, buy_price, sell_price, step_pct,
              buy_splits, sell_splits, cash, confirm: str = "") -> dict:
        with self.lock:
            if self.trader is not None and self.thread and self.thread.is_alive():
                return {"error": "이미 실행 중이다. 먼저 중지해야 한다."}
            cfg = load_config(self.config_path)
            mock = cfg["mode"] != "real"
            if not mock and confirm != "YES":
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 확인란에 YES를 입력해야 시작된다."}
            code = (code or "").strip().upper()
            try:
                split_cfg = SplitConfig(
                    code=code,
                    buy_start_price=_parse_float(buy_price, "매수 시작 금액"),
                    sell_start_price=_parse_float(sell_price, "매도 시작 금액"),
                    step_pct=_parse_float(step_pct, "분할 %"),
                    buy_splits=_parse_int(buy_splits, "매수 분할"),
                    sell_splits=_parse_int(sell_splits, "매도 분할"),
                    total_cash=_parse_float(cash, "총 투입 금액"),
                    market=market,
                    allow_real=not mock,
                    us_day_session=cfg.get("us_day_session", True),
                )
                client = self.client_factory(cfg["appkey"], cfg["secretkey"],
                                             mock=mock, market=market)
                api = RecordingAPI(client)
                trader = SplitTrader(api=api, config=split_cfg,
                                     is_simulation=mock)
            except (ValueError, PermissionError) as e:
                return {"error": str(e)}

            def run_guarded():
                try:
                    trader.run()
                except Exception as e:  # 스레드가 죽어도 UI에 이유를 남긴다
                    self.last_error = f"매매 스레드 종료: {e}"

            thread = threading.Thread(target=run_guarded, daemon=True)
            self.trader, self.api, self.thread = trader, api, thread
            self.params = {"market": market, "code": code,
                           "buy_start_price": split_cfg.buy_start_price,
                           "sell_start_price": split_cfg.sell_start_price,
                           "step_pct": split_cfg.step_pct,
                           "buy_splits": split_cfg.buy_splits,
                           "sell_splits": split_cfg.sell_splits,
                           "cash": split_cfg.total_cash,
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
            "prices": [], "events": [], "fills": [], "levels": [],
            "equity": None, "cash": None, "realized_pnl": None,
            "position_qty": 0, "avg_price": 0.0, "last_price": None,
            "completed": False,
        }
        if self.api:
            with self.api.lock:
                out["prices"] = self.api.prices[-300:]
        trader = self.trader
        if trader:
            out["equity"] = trader.equity()
            out["cash"] = trader.cash
            out["realized_pnl"] = trader.realized_pnl
            out["position_qty"] = trader.position_qty
            out["avg_price"] = trader.avg_price
            out["last_price"] = trader.last_price or None
            out["completed"] = trader.completed
            out["events"] = trader.events[-30:]
            out["fills"] = [
                f"{f.timestamp.strftime('%H:%M:%S')} "
                f"{'매수' if f.side == 'buy' else '매도'} "
                f"{f.quantity}주 @{f.price:,.2f}"
                for f in trader.fills[-20:]
            ]
            out["levels"] = [
                {"seq": l.seq, "side": l.side, "price": l.price,
                 "done": l.done, "note": l.note}
                for l in trader.levels
            ]
        return out


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoTrader 분할매매</title>
<style>
:root{--bg:#12151c;--card:#1b2029;--line:#2b3342;--txt:#e6e9ef;--dim:#8b93a3;
--buy:#3b82f6;--sell:#e5484d;--acc:#f0b429;--ok:#22c55e}
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
padding:12px;margin-bottom:14px} canvas{width:100%;height:220px;display:block}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:120px}
.panel h2{font-size:14px;color:var(--dim);margin-bottom:8px}
ul{list-style:none} li{padding:3px 0;border-bottom:1px solid var(--line);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}
th{color:var(--dim);font-weight:400} td:first-child,th:first-child{text-align:left}
.b{color:var(--buy)} .s{color:var(--sell)} .done{color:var(--dim)}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.mock{background:#14532d;color:#86efac} .badge.real{background:#7f1d1d;color:#fecaca}
#realConfirm{display:none} .warn{color:#fca5a5;font-size:12px;margin:6px 0}
.okmsg{color:var(--ok)}
</style></head><body>
<h1>AutoTrader 분할매매 <span id="modeBadge" class="badge mock">모의투자</span></h1>
<div class="sub">매수 시작가에서부터 %씩 내려가며 분할매수 · 매도 시작가에서부터 %씩 올라가며 분할매도</div>

<div class="row">
  <select id="market"><option value="kr">국내주식</option><option value="us">미국주식</option></select>
  <span><label>종목코드</label><br><input id="code" value="005930" size="8"></span>
  <span><label>매수 시작가</label><br><input id="buyStart" value="" size="9" placeholder="예: 68000" title="가격이 이 금액 이하로 오면 1차 매수, 이후 분할 %씩 내려갈 때마다 추가 매수"></span>
  <span><label>매도 시작가</label><br><input id="sellStart" value="" size="9" placeholder="예: 72000" title="가격이 이 금액 이상으로 오면 1차 매도, 이후 분할 %씩 올라갈 때마다 추가 매도"></span>
  <span><label>분할 %</label><br><input id="step" value="3" size="4"></span>
  <span><label>매수 분할</label><br><input id="nbuy" value="3" size="3"></span>
  <span><label>매도 분할</label><br><input id="nsell" value="3" size="3"></span>
  <span><label>총 투입 금액</label><br><input id="cash" value="1000000" size="10"></span>
  <span id="realConfirm"><label>실전 확인</label><br><input id="confirm" placeholder="YES 입력" size="6"></span>
  <button class="primary" onclick="start()">시작</button>
  <button class="danger" onclick="stopT()">중지</button>
</div>
<div class="warn" id="msg"></div>

<div class="grid">
  <div class="card"><div class="k">상태</div><div class="v" id="stRun">대기</div></div>
  <div class="card"><div class="k">현재가</div><div class="v" id="stPrice">-</div></div>
  <div class="card"><div class="k">보유수량</div><div class="v" id="stPos">-</div></div>
  <div class="card"><div class="k">평균단가</div><div class="v" id="stAvg">-</div></div>
  <div class="card"><div class="k">실현손익</div><div class="v" id="stPnl">-</div></div>
  <div class="card"><div class="k">평가액</div><div class="v" id="stEquity">-</div></div>
</div>

<div id="chartWrap"><canvas id="chart" width="1040" height="220"></canvas></div>

<div class="cols">
  <div class="panel"><h2>분할 계획</h2>
    <table><thead><tr><th>구분</th><th>발동가</th><th>상태</th></tr></thead>
    <tbody id="levels"><tr><td colspan="3">시작 전</td></tr></tbody></table>
  </div>
  <div class="panel"><h2>체결 / 이벤트</h2><ul id="log"><li>기록 없음</li></ul></div>
</div>

<script>
const $=id=>document.getElementById(id);
$("market").onchange=()=>{ const us=$("market").value==="us";
  $("code").value = us ? "AAPL" : "005930";
  $("cash").value = us ? "1000" : "1000000"; };
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",
headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
return r.json();}
async function start(){
  const r=await api("/api/start",{market:$("market").value,code:$("code").value.trim(),
    buy_price:$("buyStart").value.trim(),sell_price:$("sellStart").value.trim(),
    step_pct:$("step").value.trim(),
    buy_splits:$("nbuy").value.trim(),sell_splits:$("nsell").value.trim(),
    cash:$("cash").value.trim(),confirm:$("confirm").value.trim()});
  $("msg").textContent=r.error||"분할매매 시작 - 장중에 시작 금액 도달 시 자동 집행됩니다";
  $("msg").className=r.error?"warn":"warn okmsg";
}
async function stopT(){const r=await api("/api/stop",{});$("msg").textContent=r.error||"중지 요청 완료";}
function drawChart(prices,levels){
  const c=$("chart"),x=c.getContext("2d");x.clearRect(0,0,c.width,c.height);
  if(prices.length<2){x.fillStyle="#8b93a3";x.fillText("시세 수집 대기 중 (장중에만 수집됩니다)",20,40);return;}
  const vals=prices.map(p=>p[1]);
  let min=Math.min(...vals),max=Math.max(...vals);
  (levels||[]).forEach(l=>{min=Math.min(min,l.price);max=Math.max(max,l.price);});
  const pad=(max-min)||1, py=v=>15+(c.height-40)*(1-(v-min)/pad);
  (levels||[]).forEach(l=>{x.strokeStyle=l.side==="buy"?"#3b82f6":"#e5484d";
    x.globalAlpha=l.done?0.25:0.7;x.setLineDash([4,4]);x.beginPath();
    x.moveTo(30,py(l.price));x.lineTo(c.width-20,py(l.price));x.stroke();});
  x.setLineDash([]);x.globalAlpha=1;
  x.strokeStyle="#f0b429";x.lineWidth=1.6;x.beginPath();
  prices.forEach((p,i)=>{const px=30+(c.width-50)*i/(prices.length-1);
    i?x.lineTo(px,py(p[1])):x.moveTo(px,py(p[1]));});
  x.stroke();
  x.fillStyle="#8b93a3";x.fillText(max.toLocaleString(),4,18);x.fillText(min.toLocaleString(),4,c.height-22);
}
async function refresh(){
  try{
    const s=await api("/api/status");
    const real=s.config_mode==="real";
    $("modeBadge").textContent=real?"실전투자":"모의투자";
    $("modeBadge").className="badge "+(real?"real":"mock");
    $("realConfirm").style.display=real?"inline-block":"none";
    $("stRun").textContent=s.completed?"완료":(s.running?"실행 중":"대기");
    $("stPrice").textContent=s.last_price?s.last_price.toLocaleString():"-";
    $("stPos").textContent=s.position_qty+"주";
    $("stAvg").textContent=s.avg_price?s.avg_price.toLocaleString(undefined,{maximumFractionDigits:2}):"-";
    $("stPnl").textContent=s.realized_pnl!=null?Math.round(s.realized_pnl).toLocaleString():"-";
    $("stEquity").textContent=s.equity!=null?Math.round(s.equity).toLocaleString():"-";
    drawChart(s.prices,s.levels);
    if(s.levels&&s.levels.length){
      $("levels").innerHTML=s.levels.map(l=>
        "<tr class='"+(l.done?"done":"")+"'><td class='"+(l.side==="buy"?"b":"s")+"'>"+
        l.seq+"차 "+(l.side==="buy"?"매수":"매도")+"</td><td>"+
        l.price.toLocaleString(undefined,{maximumFractionDigits:2})+"</td><td>"+
        (l.done?(l.note||"완료"):"대기")+"</td></tr>").join("");
    }
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
                    code=body.get("code", ""),
                    buy_price=body.get("buy_price"),
                    sell_price=body.get("sell_price"),
                    step_pct=body.get("step_pct"),
                    buy_splits=body.get("buy_splits", 3),
                    sell_splits=body.get("sell_splits", 3),
                    cash=body.get("cash"),
                    confirm=body.get("confirm", ""),
                ))
            elif self.path == "/api/stop":
                self._send(self.state.stop())
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
    print(f"AutoTrader 분할매매 대시보드: {url}  (종료: Ctrl+C)")
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
