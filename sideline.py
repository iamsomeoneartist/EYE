"""
Bithumb Squeeze Momentum + RSI Auto-Trading Bot
------------------------------------------------
LIVE_TRADING 환경변수가 "true"가 아니면 항상 Paper(모의) 모드로 동작합니다.
실거래를 켜기 전 반드시 최소 금액으로 충분히 검증하세요.
"""

import os
import re
import json
import time
import hmac
import hashlib
import base64
import datetime
import urllib.parse

import requests
import pandas as pd
import pandas_ta_classic as ta  # 원조 pandas_ta는 유지보수 중단(구버전 삭제)됨 -> 커뮤니티 포크로 교체

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

BITHUMB_ACCESS_KEY = os.getenv("BITHUMB_ACCESS_KEY", "")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY", "")
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"

TOP_VOL_COUNT = 15
MAX_DAILY_TRADES = 2
MAX_DAILY_LOSSES = 2
MIN_ORDER_VALUE = 5000  # 빗썸 최소 주문금액(원)

# --- EYE.py에서 흡수한 장점 1: 리스크 기반 포지션 사이징 ---
# 고정 금액 대신 "허용 손실액 ÷ (진입가-손절가)"로 사이즈를 계산한다.
INITIAL_SEED = float(os.getenv("INITIAL_SEED", "100000"))       # 최초 가상/기준 시드
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "0.015"))  # 1회 최대 허용 손실 비율(잔고 대비)
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.5"))  # 1회 주문이 잔고의 최대 몇 %까지 가능한지
LOGS_DIR = "logs"


# ------------------------------------------------------------------
# EYE.py에서 흡수한 장점 1: 리스크 기반 포지션 사이징
# ------------------------------------------------------------------
def calculate_position_krw(balance, entry_price, stop_loss_price):
    """포지션 크기(원화) = (잔고 * 1회 허용 손실비율) / (진입가-손절가 폭)
    잔고의 MAX_POSITION_PCT를 넘지 않도록 상한을 두고, 최소 주문금액 미달이면 0 반환."""
    risk_per_unit = entry_price - stop_loss_price
    if risk_per_unit <= 0:
        return 0.0

    target_max_loss = balance * MAX_RISK_PER_TRADE
    calculated_value = (target_max_loss / risk_per_unit) * entry_price
    capped_value = min(calculated_value, balance * MAX_POSITION_PCT)

    return capped_value if capped_value >= MIN_ORDER_VALUE else 0.0


# ------------------------------------------------------------------
# EYE.py에서 흡수한 장점 2: 실제 매매일지 CSV 축적
# ------------------------------------------------------------------
def append_trade_journal(record):
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, "trade_journal.csv")
    file_exists = os.path.exists(path)
    df_row = pd.DataFrame([record])
    df_row.to_csv(path, mode="a", header=not file_exists, index=False)


# ------------------------------------------------------------------
# Discord
# ------------------------------------------------------------------
def send_discord_msg(title, description, color=3447003):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Discord Webhook Error: {e}")


# ------------------------------------------------------------------
# Bithumb Private API (실주문) - 구버전 REST API 기준
# LIVE_TRADING=false 이면 이 함수들은 호출은 되지만 실제 요청을 보내지 않고
# 성공한 것처럼 시뮬레이션만 합니다 (Paper Trading).
# ------------------------------------------------------------------
def _bithumb_private_request(endpoint, params):
    """빗썸 Private API 서명 및 호출. 실패 시 (False, 에러메시지) 반환."""
    if not BITHUMB_ACCESS_KEY or not BITHUMB_SECRET_KEY:
        return False, "API 키 미설정"

    url = f"https://api.bithumb.com{endpoint}"
    nonce = str(int(time.time() * 1000))
    params["endpoint"] = endpoint

    query_string = urllib.parse.urlencode(params)
    message = endpoint + chr(0) + query_string + chr(0) + nonce
    signature = hmac.new(
        BITHUMB_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha512
    ).hexdigest()
    api_sign = base64.b64encode(signature.encode("utf-8"))

    headers = {
        "Api-Key": BITHUMB_ACCESS_KEY,
        "Api-Sign": api_sign,
        "Api-Nonce": nonce,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        res = requests.post(url, headers=headers, data=params, timeout=5)
        data = res.json()
        if data.get("status") == "0000":
            return True, data
        return False, data.get("message", "unknown error")
    except Exception as e:
        return False, str(e)


def place_buy_order(symbol, krw_amount, price):
    """symbol 예: 'BTC_KRW'. price 기준 지정가 매수 시도."""
    coin = symbol.replace("_KRW", "")
    units = round(krw_amount / price, 8)

    if not LIVE_TRADING:
        print(f"[PAPER] BUY {coin} units={units} price={price}")
        return True, {"paper": True, "units": units}

    params = {
        "order_currency": coin,
        "payment_currency": "KRW",
        "units": units,
        "price": int(price),
        "type": "bid"
    }
    return _bithumb_private_request("/trade/place", params)


def place_sell_order(symbol, units, price):
    """지정가 매도 (일반적으로는 익절 등 가격이 중요한 경우에만 사용)."""
    coin = symbol.replace("_KRW", "")

    if not LIVE_TRADING:
        print(f"[PAPER] SELL(limit) {coin} units={units} price={price}")
        return True, {"paper": True}

    params = {
        "order_currency": coin,
        "payment_currency": "KRW",
        "units": units,
        "price": int(price),
        "type": "ask"
    }
    return _bithumb_private_request("/trade/place", params)


def place_market_sell_order(symbol, units):
    """시장가 매도. 손절/긴급청산처럼 체결 보장이 가격보다 중요할 때 사용.
    /trade/market_sell 은 price 파라미터가 없다 (공식 문서 확인)."""
    coin = symbol.replace("_KRW", "")

    if not LIVE_TRADING:
        print(f"[PAPER] SELL(market) {coin} units={units}")
        return True, {"paper": True}

    params = {
        "units": units,
        "order_currency": coin,
        "payment_currency": "KRW",
    }
    return _bithumb_private_request("/trade/market_sell", params)


# ------------------------------------------------------------------
# 시세 조회
# ------------------------------------------------------------------
def get_bithumb_ohlcv(symbol, interval="5m", retries=3):
    url = f"https://api.bithumb.com/public/candlestick/{symbol}/{interval}"
    for attempt in range(retries):
        try:
            res = requests.get(url, timeout=5).json()
            if res.get("status") == "0000":
                df = pd.DataFrame(
                    res["data"],
                    columns=["time", "open", "close", "high", "low", "volume"]
                )
                for col in ["open", "close", "high", "low", "volume"]:
                    df[col] = df[col].astype(float)
                return df
        except Exception as e:
            print(f"OHLCV fetch error ({symbol}, try {attempt+1}): {e}")
            time.sleep(0.5 * (attempt + 1))  # exponential backoff
    return None


def get_withdrawal_blacklist():
    """
    실제 존재하는 엔드포인트로 교체:
    /public/assetsstatus/multichain/{currency} (currency=ALL 지원)
    이전 코드가 참조하던 ticker의 'is_frozen' 필드는 실제 API 응답에 존재하지 않아
    한 번도 작동하지 않았음 -> 실제 입출금 상태 API로 교체.
    출금(withdrawal_status)이 막힌 네트워크가 하나라도 있으면 보수적으로 블랙리스트에 추가.
    """
    blacklist = set()
    url = "https://api.bithumb.com/public/assetsstatus/multichain/ALL"
    try:
        res = requests.get(url, timeout=5).json()
        data = res.get("data")
        if isinstance(data, list):
            for entry in data:
                currency = entry.get("currency")
                if currency and str(entry.get("withdrawal_status")) == "0":
                    blacklist.add(f"{currency}_KRW")
        elif isinstance(data, dict):
            for currency, entries in data.items():
                nets = entries if isinstance(entries, list) else [entries]
                if any(str(n.get("withdrawal_status")) == "0" for n in nets if isinstance(n, dict)):
                    blacklist.add(f"{currency}_KRW")
    except Exception as e:
        print(f"Withdrawal status fetch error: {e}")
    return blacklist


def get_all_krw_tickers():
    """ALL_KRW 조회 1회로 스크리닝 대상을 얻는다."""
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    for attempt in range(3):
        try:
            res = requests.get(url, timeout=5).json()
            if res.get("status") == "0000":
                data = res["data"]
                data.pop("date", None)
                return data
        except Exception as e:
            print(f"Ticker fetch error (try {attempt+1}): {e}")
            time.sleep(0.5 * (attempt + 1))
    return {}


def build_candidates_and_blacklist(ticker_data):
    blacklist = get_withdrawal_blacklist()
    entries = []
    for symbol, info in ticker_data.items():
        if not isinstance(info, dict):
            continue
        try:
            entries.append((f"{symbol}_KRW", float(info.get("acc_trade_value_24H", 0))))
        except (TypeError, ValueError):
            continue
    entries.sort(key=lambda x: x[1], reverse=True)
    candidates = [s for s, _ in entries[:TOP_VOL_COUNT] if s not in blacklist]
    return candidates, blacklist


# ------------------------------------------------------------------
# 지표 계산 (pandas_ta 버전별 컬럼명 차이에 안전하게 접근)
# ------------------------------------------------------------------
def _col(df, prefix):
    """prefix로 시작하는 첫 번째 컬럼을 찾는다. 버전별 접미사 차이 대응."""
    matches = [c for c in df.columns if c.startswith(prefix)]
    if not matches:
        return None
    return matches[0]


def is_btc_macro_bull():
    btc_df = get_bithumb_ohlcv("BTC_KRW", interval="5m")
    if btc_df is None or len(btc_df) < 200:
        return True  # 데이터 없으면 차단하지 않음(보수적으로 통과)

    btc_df["ema50"] = ta.ema(btc_df["close"], length=50)
    btc_df["ema200"] = ta.ema(btc_df["close"], length=200)

    curr = btc_df.iloc[-1]
    prev = btc_df.iloc[-2]

    if pd.isna(curr["ema50"]) or pd.isna(curr["ema200"]):
        return True

    btc_change = (curr["close"] - prev["close"]) / prev["close"]
    if btc_change < -0.015 or curr["close"] < curr["ema50"] or curr["ema50"] < curr["ema200"]:
        return False
    return True


def analyze_symbol(symbol):
    df = get_bithumb_ohlcv(symbol)
    if df is None or len(df) < 200:
        return None

    try:
        df["rsi"] = ta.rsi(df["close"], length=14).fillna(50)
        df["rsi_sma"] = ta.sma(df["rsi"], length=9).fillna(50)
        df["ema50"] = ta.ema(df["close"], length=50).fillna(df["close"])
        df["ema200"] = ta.ema(df["close"], length=200).fillna(df["close"])
        df["vol_sma"] = ta.sma(df["volume"], length=20).fillna(1)
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14).fillna(0)

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = _col(adx_df, "ADX_") if adx_df is not None else None
        df["adx"] = adx_df[adx_col].fillna(0) if adx_col else 0

        bb = ta.bbands(df["close"], length=20, std=2.0)
        kc = ta.kc(df["high"], df["low"], df["close"], length=20, scalar=1.5)
        if bb is None or kc is None:
            return None

        bbl_col, bbu_col = _col(bb, "BBL_"), _col(bb, "BBU_")
        kcl_col, kcu_col = _col(kc, "KCL"), _col(kc, "KCU")
        if not all([bbl_col, bbu_col, kcl_col, kcu_col]):
            return None

        df["squeeze_on"] = (bb[bbl_col] > kc[kcl_col]) & (bb[bbu_col] < kc[kcu_col])
        df["squeeze_off"] = (bb[bbl_col] < kc[kcl_col]) & (bb[bbu_col] > kc[kcu_col])

        highest_high = df["high"].rolling(20).max()
        lowest_low = df["low"].rolling(20).min()
        sma_close = ta.sma(df["close"], length=20)
        kc_mid = (highest_high + lowest_low + sma_close) / 3
        df["mom"] = ta.linreg(df["close"] - kc_mid, length=20).fillna(0)
    except Exception as e:
        print(f"Indicator calc error ({symbol}): {e}")
        return None

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    now_hour = datetime.datetime.now().hour
    session_filter = not (2 <= now_hour <= 5)

    recent_high = df["high"].iloc[-21:-1].max()
    breakout_filter = curr["close"] > (recent_high * 1.015)
    adx_filter = curr["adx"] > 25
    volatility_ratio = (curr["atr"] / curr["close"]) > 0.015 if curr["close"] else False

    squeeze_trigger = bool(prev["squeeze_on"]) and bool(curr["squeeze_off"]) and (curr["mom"] > 0)
    rsi_trigger = (prev["rsi"] < 30 and curr["rsi"] >= 30) or \
                  (prev["rsi"] < prev["rsi_sma"] and curr["rsi"] > curr["rsi_sma"])
    trend_filter = curr["close"] > curr["ema50"] and curr["ema50"] > curr["ema200"]
    volume_filter = curr["volume"] > (curr["vol_sma"] * 2.0)

    is_buyable = (squeeze_trigger or rsi_trigger) and trend_filter and volume_filter \
        and session_filter and breakout_filter and adx_filter and volatility_ratio

    # --- EYE.py에서 흡수한 장점 3: 사람이 읽을 수 있는 판단 근거 ---
    if is_buyable:
        eval_reason = "🟢 매수 조건 완벽 수렴"
    elif not trend_filter:
        eval_reason = "추세 조건 미충족 (EMA 역배열)"
    elif not volume_filter:
        eval_reason = "거래량 부족"
    elif not adx_filter:
        eval_reason = "추세 강도(ADX) 약함"
    elif not breakout_filter:
        eval_reason = "저항 돌파 미확인"
    elif not (squeeze_trigger or rsi_trigger):
        eval_reason = "스퀴즈/RSI 트리거 대기 중"
    else:
        eval_reason = "조건 일부만 충족, 관망"

    stop_loss_estimate = curr["close"] - (1.5 * curr["atr"]) if curr["atr"] > 0 else curr["close"] * 0.98

    return {
        "symbol": symbol,
        "is_buyable": is_buyable,
        "eval_reason": eval_reason,
        "price": curr["close"],
        "atr": curr["atr"],
        "rsi": curr["rsi"],
        "mom": curr["mom"],
        "stop_loss_estimate": stop_loss_estimate,
    }


# ------------------------------------------------------------------
# 상태 관리
# ------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "position": "NONE",
        "target_symbol": None,
        "buy_price": 0,
        "units": 0,
        "highest_price": 0,
        "daily_loss_count": 0,
        "daily_trade_count": 0,
        "consecutive_losses": 0,
        "last_reset_date": "",
        "balance": INITIAL_SEED,   # EYE.py에서 흡수: 실제 계산된 가상/기준 잔고 (지어낸 수치 아님)
        "last_scan": [],          # EYE.py에서 흡수: 최근 스캔 후보별 판단 근거
    }


def save_state(state):
    state["mode"] = "LIVE" if LIVE_TRADING else "PAPER"
    state["last_run_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)


def reset_daily_counters_if_needed(state):
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today_str:
        state["daily_loss_count"] = 0
        state["daily_trade_count"] = 0
        state["last_reset_date"] = today_str
    return state


# ------------------------------------------------------------------
# 포지션 관리 (버그 수정: 이 로직은 일일 락아웃과 무관하게 항상 실행되어야 함)
# ------------------------------------------------------------------
def manage_open_position(state, blacklist):
    target_symbol = state["target_symbol"]

    if target_symbol in blacklist:
        emergency_info = analyze_symbol(target_symbol)
        exit_price = emergency_info["price"] if emergency_info else state["buy_price"]
        ok, resp = place_market_sell_order(target_symbol, state.get("units", 0))
        _record_exit(state, target_symbol, exit_price, "BLACKLIST", ok)
        send_discord_msg(
            f"🚨 [긴급 비상 매도] {target_symbol}",
            f"**사유**: 입출금 정지/유의 감지\n주문 결과: {'성공' if ok else f'실패({resp})'}\n"
            f"**잔고**: {state['balance']:,.0f} KRW",
            color=15158332
        )
        _clear_position(state)
        return state

    analysis = analyze_symbol(target_symbol)
    if analysis is None:
        return state  # 데이터 실패 시 다음 주기에 재시도, 상태는 그대로 유지

    current_price = analysis["price"]
    current_atr = analysis["atr"]

    if current_price > state["highest_price"]:
        state["highest_price"] = current_price

    stop_loss_price = state["buy_price"] - (1.5 * current_atr) if current_atr > 0 else state["buy_price"] * 0.980
    trailing_price = state["highest_price"] - (2.0 * current_atr) if current_atr > 0 else state["highest_price"] * 0.985

    # Breakeven Lock: 최고가가 진입가 대비 +5.0% 이상이면 손절선을 수수료 보정 구간(+2.5%)까지 상향
    if state["highest_price"] >= state["buy_price"] * 1.050:
        stop_loss_price = max(stop_loss_price, state["buy_price"] * 1.025)

    # 대시보드 상세보기용: 지금 계산된 손절/추적손절선과 현재가를 저장
    state["current_price"] = current_price
    state["current_stop_loss"] = round(stop_loss_price, 2)
    state["current_trailing_stop"] = round(trailing_price, 2)
    state["current_rsi"] = round(float(analysis["rsi"]), 2)

    should_exit = (
        current_price <= stop_loss_price
        or current_price <= trailing_price
        or analysis["rsi"] > 80
        or analysis["mom"] < 0
    )

    if should_exit:
        ok, resp = place_market_sell_order(target_symbol, state.get("units", 0))
        is_profit = current_price > (state["buy_price"] * 1.025)
        _record_exit(state, target_symbol, current_price, "TP" if is_profit else "SL", ok)

        if not is_profit:
            state["daily_loss_count"] = state.get("daily_loss_count", 0) + 1
            state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        else:
            state["consecutive_losses"] = 0

        send_discord_msg(
            f"🔴 [청산] {target_symbol}",
            f"**청산가**: {current_price:,.0f} KRW\n**진입가**: {state['buy_price']:,.0f} KRW\n"
            f"**결과**: {'익절' if is_profit else '손절'}\n**주문**: {'성공' if ok else f'실패({resp})'}\n"
            f"**잔고**: {state['balance']:,.0f} KRW",
            color=3066993 if is_profit else 15158332
        )
        _clear_position(state)

    return state


def _record_exit(state, symbol, exit_price, reason, order_ok):
    """EYE.py에서 흡수: 실제 계산된 손익을 잔고에 반영하고 매매일지 CSV에 기록."""
    units = state.get("units", 0)
    buy_price = state.get("buy_price", 0)
    pnl_krw = (exit_price - buy_price) * units
    pnl_pct = ((exit_price / buy_price) - 1) * 100 if buy_price else 0

    state["balance"] = state.get("balance", INITIAL_SEED) + pnl_krw

    append_trade_journal({
        "symbol": symbol,
        "entry_time": state.get("entry_time", ""),
        "entry_price": buy_price,
        "exit_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_price": exit_price,
        "units": units,
        "pnl_krw": round(pnl_krw, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "order_ok": order_ok,
        "balance_after": round(state["balance"], 2),
    })


def _clear_position(state):
    state["position"] = "NONE"
    state["target_symbol"] = None
    state["buy_price"] = 0
    state["units"] = 0
    state["highest_price"] = 0
    state["current_price"] = 0
    state["current_stop_loss"] = 0
    state["current_trailing_stop"] = 0
    state["current_rsi"] = 0


# ------------------------------------------------------------------
# 메인 루프
# ------------------------------------------------------------------
def run():
    state = load_state()
    state = reset_daily_counters_if_needed(state)

    ticker_data = get_all_krw_tickers()
    candidates, blacklist = build_candidates_and_blacklist(ticker_data)

    # 1) 보유 포지션이 있으면 락아웃 여부와 무관하게 항상 감시/청산 로직을 먼저 실행
    if state["position"] == "BUY":
        state = manage_open_position(state, blacklist)
        save_state(state)
        return

    # 2) 신규 진입은 일일 손절/매매 횟수 제한에 걸리면 여기서만 차단
    if state.get("daily_loss_count", 0) >= MAX_DAILY_LOSSES or \
       state.get("daily_trade_count", 0) >= MAX_DAILY_TRADES:
        save_state(state)
        return

    if not is_btc_macro_bull():
        save_state(state)
        return

    scan_summary = []
    for symbol in candidates:
        time.sleep(0.2)
        res = analyze_symbol(symbol)
        if res is None:
            continue

        scan_summary.append({
            "symbol": symbol,
            "reason": res["eval_reason"],
            "price": res["price"],
            "rsi": round(float(res["rsi"]), 2),
            "momentum": round(float(res["mom"]), 2),
            "atr": round(float(res["atr"]), 2),
            "stop_loss_estimate": round(float(res["stop_loss_estimate"]), 2),
            "is_buyable": bool(res["is_buyable"]),
        })

        if state["position"] == "NONE" and res["is_buyable"]:
            price = res["price"]
            stop_loss_estimate = res["stop_loss_estimate"]

            # --- EYE.py에서 흡수: 리스크 기반 사이징 (고정금액이 아니라 손절폭 기준) ---
            position_krw = calculate_position_krw(state.get("balance", INITIAL_SEED), price, stop_loss_estimate)
            if position_krw <= 0:
                continue  # 손절폭이 너무 좁거나 최소 주문금액 미달 -> 이 후보는 건너뜀

            units = round(position_krw / price, 8)
            ok, resp = place_buy_order(symbol, position_krw, price)

            if not ok:
                send_discord_msg(f"⚠️ [매수 주문 실패] {symbol}", f"사유: {resp}", color=15158332)
                continue

            state["position"] = "BUY"
            state["target_symbol"] = symbol
            state["buy_price"] = price
            state["units"] = units
            state["highest_price"] = price
            state["entry_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1

            mode = "LIVE" if LIVE_TRADING else "PAPER"
            send_discord_msg(
                f"🟢 [{mode} 신규 매수] {symbol}",
                f"**진입가**: {price:,.0f} KRW\n**주문금액**: {position_krw:,.0f} KRW "
                f"(잔고 {MAX_RISK_PER_TRADE*100:.1f}% 리스크 기준 산정)\n"
                f"**RSI**: {res['rsi']:.1f}\n**금일 매매**: {state['daily_trade_count']}/{MAX_DAILY_TRADES}회",
                color=3066993
            )

    state["last_scan"] = scan_summary[:15]  # EYE.py에서 흡수: 대시보드용 후보별 판단 근거
    save_state(state)


if __name__ == "__main__":
    run()
